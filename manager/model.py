"""映射校验、持久化及本机 Caddy 配置生成。"""
from __future__ import annotations

import ipaddress
import json
import re
import ssl
import uuid
from pathlib import Path

CONFLICT_REASON = "地址或兼容入口已被占用，请检查其他电脑或本机映射"
PENDING_REASON = "旧连接未释放，暂时无法核对。请更新旧版或稍后重新启用"

def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def load_settings(root: Path) -> dict:
    settings = json.loads((root / "settings.json").read_text("utf-8-sig"))
    defaults = {"ManagerPort": 17890, "LocalRouterPort": 17891,
                "LocalRouterAdminPort": 17892, "DirectServerIP": "203.0.113.10",
                "SharedTestEnabled": False, "SharedMainPort": 18081}
    for name, default in defaults.items():
        settings.setdefault(name, default)
    if type(settings["SharedTestEnabled"]) is not bool:
        raise ValueError("SharedTestEnabled 必须为布尔值。")
    main_port = settings["SharedMainPort"]
    if type(main_port) is not int or not 1024 <= main_port <= 65535:
        raise ValueError("SharedMainPort 必须是 1024～65535 的整数。")
    if settings["SharedTestEnabled"] and main_port in {settings["FrpsBindPort"], settings["RemotePort"]}:
        raise ValueError("协作模式的普通入口不能与原入口或控制端口重复。")
    ports = [settings[name] for name in ("ManagerPort", "LocalRouterPort", "LocalRouterAdminPort")]
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports):
        raise ValueError("管理界面和路由端口必须是 1024～65535 的整数。")
    if len(set(ports)) != 3 or set(ports) & {80, 443, settings["FrpsBindPort"], settings["RemotePort"]}:
        raise ValueError("管理界面、路径路由、管理接口和 FRP 端口不能重复。")
    if settings["SharedTestEnabled"] and main_port in ports:
        raise ValueError("协作模式的普通入口不能与本机管理端口重复。")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", settings["PublicDomain"]):
        raise ValueError("公网域名格式不正确。")
    ip = ipaddress.IPv4Address(settings["DirectServerIP"])
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip in ipaddress.ip_network("198.18.0.0/15"):
        raise ValueError("直连地址必须使用真实服务器 IPv4，不能使用 Fake-IP。")
    return settings


def validate_mapping(value: dict, settings: dict, *, mapping_id: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("映射必须是一个对象。")
    name = value.get("name", "")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("请输入 1～80 字的映射名称。")
    port = value.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("本地端口必须是 1～65535 的整数。")
    if port in {settings["ManagerPort"], settings["LocalRouterPort"], settings["LocalRouterAdminPort"]}:
        raise ValueError("不能把管理界面或路由管理接口发布到公网。")
    prefix = value.get("path", "/")
    if not isinstance(prefix, str):
        raise ValueError("路径格式不正确。")
    prefix = "/" + prefix.strip().strip("/")
    if prefix != "/" and not re.fullmatch(r"/[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", prefix):
        raise ValueError("路径只支持一层英文、数字、下划线或短横线，例如 /demo。")
    hostname = value.get("hostname", "")
    if not isinstance(hostname, str):
        raise ValueError("独立域名格式不正确。")
    hostname = hostname.strip().lower().rstrip(".")
    if hostname:
        parent = settings["ServerAddress"].lower().rstrip(".")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\." + re.escape(parent), hostname):
            raise ValueError("独立域名须为服务器域名下的一级子域名，例如 web.example.com。")
        if hostname == settings["PublicDomain"].lower().rstrip("."):
            raise ValueError("该域名已用于原有路径映射。")
        if prefix != "/":
            raise ValueError("独立域名映射的访问路径必须为 /。")
    enabled = value.get("enabled", True)
    strip_prefix = value.get("strip_prefix", True)
    if type(enabled) is not bool or type(strip_prefix) is not bool:
        raise ValueError("启用和前缀选项必须为布尔值。")
    scheme=value.get("scheme","http")
    if not isinstance(scheme,str) or scheme not in {"http","https"}:raise ValueError("本地协议只能选择 HTTP 或 HTTPS。")
    ca_file=value.get("tls_ca_file","")
    if not isinstance(ca_file,str) or len(ca_file)>1024 or any(ord(char)<32 or char in "{}" for char in ca_file):
        raise ValueError("根证书文件位置不正确。")
    ca_file=ca_file.strip()
    if ca_file and scheme!="https":raise ValueError("根证书只用于 HTTPS 后端。")
    if ca_file:
        certificate=Path(ca_file)
        if not certificate.is_absolute() or not certificate.is_file():raise ValueError("请选择存在的根证书文件绝对路径。")
        try:ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cafile=str(certificate))
        except (ssl.SSLError,OSError):raise ValueError("根证书需要是有效的 PEM 格式公开证书。") from None
        ca_file=certificate.resolve().as_posix()
    result={"id": mapping_id or uuid.uuid4().hex, "name": name.strip(), "port": port,
            "path": prefix, "enabled": enabled, "strip_prefix": strip_prefix}
    if not enabled and value.get("disabled_reason") in {CONFLICT_REASON, PENDING_REASON, "地址已由其他电脑使用", "无法核对服务器，请稍后重新启用"}:
        result["disabled_reason"] = value["disabled_reason"]
    if hostname:
        result["hostname"] = hostname
    # 旧 HTTP 映射的保存格式和行为保持原样。
    if scheme=="https":
        result["scheme"]=scheme
        if ca_file:result["tls_ca_file"]=ca_file
    return result


def validate_mappings(mappings: list, settings: dict) -> list:
    if not isinstance(mappings, list):
        raise ValueError("映射列表格式不正确。")
    result = []
    paths, ids = set(), set()
    for value in mappings:
        mapping_id = value.get("id") if isinstance(value, dict) else None
        if not isinstance(mapping_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", mapping_id):
            raise ValueError("映射记录缺少有效 ID。")
        item = validate_mapping(value, settings, mapping_id=mapping_id)
        route = (item.get("hostname", ""), item["path"])
        if route in paths or item["id"] in ids:
            raise ValueError("同一个路径只能添加一次，请编辑已有映射。")
        paths.add(route)
        ids.add(item["id"])
        result.append(item)
    return result


class MappingStore:
    def __init__(self, path: Path, settings: dict):
        self.path = path
        self.settings = settings
        if not path.exists():
            self.save([validate_mapping({"name": "默认网站", "port": settings["LocalPort"],
                                         "path": "/"}, settings, mapping_id="home")])
        self.load()

    def load(self) -> list:
        return validate_mappings(json.loads(self.path.read_text("utf-8-sig")), self.settings)

    def save(self, mappings: list) -> None:
        mappings = validate_mappings(mappings, self.settings)
        atomic_write(self.path, json.dumps(mappings, ensure_ascii=False, indent=2) + "\n")


def proxy_lines(item: dict, indent: str, public_domain: str) -> list[str]:
    address=f"127.0.0.1:{item['port']}"
    if item.get("scheme","http")=="http":return [indent+"reverse_proxy "+address]
    lines=[indent+"reverse_proxy https://"+address+" {",indent+"  header_up Host "+address]
    # 仅转换配置的公网 HTTPS 来源；其他来源保留，继续交给应用拒绝。
    public_origin="^https://"+public_domain.lower().replace(".","[.]")+"(?::443)?$"
    lines.append(indent+"  header_up Origin "+json.dumps(public_origin)+" https://"+address)

    if item.get("tls_ca_file"):
        lines += [indent+"  transport http {",indent+"    tls_trust_pool file "+json.dumps(item["tls_ca_file"],ensure_ascii=False),indent+"  }"]
    return lines+[indent+"}"]


def caddy_config(mappings: list, settings: dict, runtime: Path) -> str:
    mappings = validate_mappings(mappings, settings)
    admin = settings["LocalRouterAdminPort"]
    logfile = json.dumps((runtime / "router.log").as_posix(), ensure_ascii=False)
    lines = ["{", f"  admin 127.0.0.1:{admin} {{",
             f"    origins http://127.0.0.1:{admin}", "    enforce_origin", "  }",
             "  auto_https off", "  persist_config off", "  servers {",
             "    trusted_proxies static 127.0.0.1/32", "    trusted_proxies_strict", "  }", "  log {",
             f"    output file {logfile} {{", "      roll_size 10MiB", "      roll_keep 3",
             "    }", "  }", "}", f"http://:{settings['LocalRouterPort']} {{",
             "  bind 127.0.0.1", "  route {"]
    active = [item for item in mappings if item["enabled"]]
    # 服务器通过隧道查询当前启用的独立域名，决定是否允许申请证书。
    lines += ["    @certificate_check {", "      host 127.0.0.1 localhost",
              "      path /__personal_web_tunnel/certificate-permission", "    }",
              "    handle @certificate_check {"]
    permitted = [item["hostname"] for item in active if item.get("hostname")]
    if permitted:
        lines += ["      @permitted query " + " ".join("domain=" + host for host in permitted),
                  '      respond @permitted "allowed" 200']
    lines += ['      respond "denied" 403', "    }"]
    for index, item in enumerate(active):
        if item.get("hostname"):
            lines += [f"    @domain{index} host {item['hostname']}", f"    handle @domain{index} {{"]
            lines += proxy_lines(item, "      ", item["hostname"]) + ["    }"]
    # 已停用或删除的独立域名不能误落到原有默认网站。
    lines += [f"    @unknown_domain not host {settings['PublicDomain']} localhost 127.0.0.1",
              "    handle @unknown_domain {", '      respond "没有匹配的域名映射" 404', "    }"]
    root = None
    for index, item in enumerate(sorted(active, key=lambda value: value["path"])):
        if item.get("hostname"):
            continue
        prefix, port = item["path"], item["port"]
        if prefix == "/":
            root = item
            continue
        lines += [f"    @exact{index} path {prefix}", f"    redir @exact{index} {prefix}/?{{query}} 308"]
        handler = "handle_path" if item["strip_prefix"] else "handle"
        lines += [f"    {handler} {prefix}/* {{"]+proxy_lines(item,"      ",settings["PublicDomain"])+["    }"]
    if settings.get("SharedTestEnabled"):
        # /test 未单独映射时不能落入默认网站，避免误显示本机根路径页面。
        lines += ["    @shared_test path /test /test/*", "    handle @shared_test {",
                  '      respond "本机未发布 /test" 404', "    }"]
    lines += ["    handle {"]
    lines += proxy_lines(root,"      ",settings["PublicDomain"]) if root else ['      respond "没有匹配的网站映射" 404']
    return "\n".join(lines + ["    }", "  }", "}", ""])
