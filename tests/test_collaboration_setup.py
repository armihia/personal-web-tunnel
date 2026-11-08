"""验证服务器迁移预览保留现有站点和 /test。"""
from pathlib import Path
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.collaboration_registry import Registry, Server


def psquote(path):
    return "'" + str(path).replace("'", "''") + "'"


class SetupTests(unittest.TestCase):
    def setUp(self):
        work = ROOT / 'tests/.work'; work.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        self.prefix = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object Text.UTF8Encoding($false); "
                       ". " + psquote(ROOT/'common/Tunnel.SharedTest.ps1') + '; . ' + psquote(ROOT/'common/Tunnel.Collaboration.ps1') + '; ')

    def transform(self, value, function, extra=''):
        source = self.work/'input.txt'; target = self.work/'output.txt'
        source.write_text(value, encoding='utf-8')
        code = self.prefix + "$value=[IO.File]::ReadAllText("+psquote(source)+"); " + function + " $value " + extra + " | Set-Content -LiteralPath " + psquote(target) + " -Encoding UTF8"
        result = subprocess.run(['powershell.exe','-NoProfile','-Command',code], capture_output=True, timeout=20)
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', 'replace'))
        return target.read_text(encoding='utf-8-sig')

    def test_frps_adds_only_private_port_range(self):
        original = 'bindPort = 7000\nproxyBindAddr = "127.0.0.1"\nallowPorts = [{ single = 18080 }, { single = 18081 }]\ntransport.tls.force = true\n'
        value = self.transform(original, 'Enable-TunnelCollaborationFrps')
        self.assertIn('{ start = 30000, end = 30255 }', value)
        self.assertIn('transport.tls.force = true', value)
        with self.assertRaises(RuntimeError):
            self.transform(original.replace('127.0.0.1','0.0.0.0'), 'Enable-TunnelCollaborationFrps')

    def test_fresh_server_configuration_supports_coordination(self):
        output=self.work/'fresh.Caddyfile'
        code=(self.prefix+'. '+psquote(ROOT/'common/Tunnel.Common.ps1')+'; . '+psquote(ROOT/'common/Tunnel.Config.ps1')+
              '; $s=Get-TunnelSettings -Path '+psquote(ROOT/'settings.json')+
              '; $base=New-TunnelCaddyConfig $s '+psquote(self.work)+
              '; Write-TunnelText '+psquote(output)+' (Enable-TunnelCollaborationCaddy $base $s.PublicDomain $s.ServerAddress $s.SharedMainPort $s.RemotePort 18083)')
        generated=subprocess.run(['powershell.exe','-NoProfile','-Command',code],capture_output=True,timeout=20)
        self.assertEqual(generated.returncode,0,generated.stderr.decode('utf-8','replace'))
        text=output.read_text(encoding='utf-8')
        self.assertIn('admin 127.0.0.1:2019',text)
        self.assertIn('forward_auth 127.0.0.1:18083',text)
        validated=subprocess.run([str(ROOT/'client/caddy.exe'),'validate','--config',str(output),'--adapter','caddyfile'],capture_output=True,timeout=20)
        self.assertEqual(validated.returncode,0,validated.stderr.decode('utf-8','replace'))

    def test_legacy_guard_fails_when_port_already_used(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0)); listener.listen()
            port=listener.getsockname()[1]
            code=self.prefix + 'Open-TunnelLegacyGuard '+str(port)+' | Out-Null'
            result=subprocess.run(['powershell.exe','-NoProfile','-Command',code], capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode,0)

    def test_legacy_guard_exclusively_holds_and_releases_free_port(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        code=(self.prefix+'$guard=Open-TunnelLegacyGuard '+str(port)+'; '
              'try { $s=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,'+str(port)+'); '
              'try { $s.Start(); throw "入口被重复占用" } catch [System.Net.Sockets.SocketException] { Write-Output "guard-ok" } '
              'finally { $s.Stop() } } finally { $guard.Dispose() }')
        result=subprocess.run(['powershell.exe','-NoProfile','-Command',code],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        self.assertIn('guard-ok',result.stdout.decode('utf-8','replace'))
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',port))

    def test_real_frpc_accepts_cooperation_proxy_configuration(self):
        token=self.work/'token.txt'; token.write_text('a'*64, encoding='utf-8')
        config=self.work/'frpc.toml'; log=self.work/'frpc.log'
        code=("$ErrorActionPreference='Stop'; . "+psquote(ROOT/'common/Tunnel.Common.ps1')+'; . '+
              psquote(ROOT/'common/Tunnel.Config.ps1')+'; $s=Get-TunnelSettings -Path '+psquote(ROOT/'settings.json')+
              '; Write-TunnelText -Path '+psquote(config)+' -Text (New-TunnelClientConfig $s 17891 '+psquote(token)+' '+psquote(log)+
              " -RemotePort 30000 -ProxyName 'personal-map-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-"+('x'*64)+"')")
        generated=subprocess.run(['powershell.exe','-NoProfile','-Command',code],capture_output=True,timeout=20)
        self.assertEqual(generated.returncode,0,generated.stderr.decode('utf-8','replace'))
        verified=subprocess.run([str(ROOT/'client/frpc.exe'),'verify','-c',str(config)],capture_output=True,timeout=20)
        self.assertEqual(verified.returncode,0,verified.stderr.decode('utf-8','replace'))
        text=config.read_text(encoding='utf-8')
        self.assertIn('transport.tls.enable = true',text)
        self.assertIn('remotePort = 30000',text)

    def test_caddy_preserves_existing_applications_and_legacy_test(self):
        original = '''{
    auto_https disable_redirects
}
example.com {
    @join path /join /join/*
    handle @join {
        reverse_proxy 127.0.0.1:8000
    }
    @existing_app path /existing-app /existing-app/*
    handle @existing_app {
        reverse_proxy 127.0.0.1:80
    }
    handle {
        respond "Not Found" 404
    }
}
demo.example.com {
    @shared_test path /test /test/*
    handle @shared_test {
        reverse_proxy 127.0.0.1:18080
    }
    handle {
        reverse_proxy 127.0.0.1:18081
    }
}
'''
        value = self.transform(original, 'Enable-TunnelCollaborationCaddy')
        root = original[original.index('example.com {'):original.index('demo.example.com {')]
        self.assertIn(root, value)
        self.assertIn('@shared_test path /test /test/*', value)
        self.assertIn('reverse_proxy 127.0.0.1:18080', value)
        self.assertIn('ask http://127.0.0.1:18083/certificate-permission', value)
        self.assertIn('request_header -X-Personal-Tunnel-Port', value)
        path = self.work/'Caddyfile'; path.write_text(value, encoding='utf-8')
        result = subprocess.run([str(ROOT/'client/caddy.exe'),'validate','--config',str(path),'--adapter','caddyfile'], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8','replace'))

        with self.assertRaises(RuntimeError):
            self.transform(original.replace('127.0.0.1:18080','127.0.0.1:19000'), 'Enable-TunnelCollaborationCaddy')

    def test_existing_automatic_and_explicit_subdomains_move_to_registry(self):
        original = '''{
    auto_https disable_redirects
    on_demand_tls {
        ask http://127.0.0.1:18081/__personal_web_tunnel/certificate-permission
    }
}
example.com {
    respond "original root" 200
}
demo.example.com {
    @shared_test path /test /test/*
    handle @shared_test {
        reverse_proxy 127.0.0.1:18080
    }
    handle {
        reverse_proxy 127.0.0.1:18081
    }
}
web.example.com {
    tls {
        issuer acme {
            disable_http_challenge
        }
    }
    reverse_proxy 127.0.0.1:18081
}
https:// {
    tls {
        on_demand
        issuer acme {
            disable_http_challenge
        }
    }
    @personal_subdomain host *.example.com
    handle @personal_subdomain {
        reverse_proxy 127.0.0.1:18081
    }
    handle {
        respond "Not Found" 404
    }
}
'''
        value = self.transform(original, 'Enable-TunnelCollaborationCaddy')
        self.assertIn('ask http://127.0.0.1:18083/certificate-permission', value)
        self.assertNotIn('127.0.0.1:18081', value)
        self.assertIn('respond "original root" 200', value)
        self.assertEqual(value.count('forward_auth 127.0.0.1:18083'), 3)
        path = self.work/'Caddyfile'; path.write_text(value, encoding='utf-8')
        result = subprocess.run([str(ROOT/'client/caddy.exe'),'validate','--config',str(path),'--adapter','caddyfile'], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8','replace'))



class MigrationTests(unittest.TestCase):
    def setUp(self):
        work=ROOT/'tests/.work'; work.mkdir(exist_ok=True)
        self.directory=tempfile.TemporaryDirectory(dir=work); self.addCleanup(self.directory.cleanup)
        self.work=Path(self.directory.name)
        shutil.copytree(ROOT/'common', self.work/'common')
        (self.work/'server').mkdir()
        for name in ('enable-collaboration.ps1','collaboration_registry.py','status-collaboration.ps1','start-server.ps1'):
            shutil.copy2(ROOT/'server'/name, self.work/'server'/name)
        shutil.copy2(ROOT/'settings.json', self.work/'settings.json')
        self.token=self.work/'server/token.txt'; self.token.write_text('a'*64, encoding='utf-8')
        self.frps=self.work/'server/frps.toml'
        self.frps.write_text('proxyBindAddr = "127.0.0.1"\nallowPorts = [{ single = 18080 }, { single = 18081 }]\nauth.tokenSource.file.path = '+json.dumps(str(self.token))+'\ntransport.tls.force = true\n', encoding='utf-8')
        (self.work/'server/frps.exe').write_text('测试替身', encoding='utf-8')
        self.caddy=self.work/'Caddyfile'
        self.caddy.write_text('''{
 auto_https disable_redirects
}
example.com {
 respond "root" 200
}
demo.example.com {
 @shared_test path /test /test/*
 handle @shared_test {
  reverse_proxy 127.0.0.1:18080
 }
 handle {
  reverse_proxy 127.0.0.1:18081
 }
}
''', encoding='utf-8')
        self.original={p:p.read_bytes() for p in (self.frps,self.caddy)}
        reg=Registry(self.work/'test.sqlite',base_domain='example.com',public_domain='demo.example.com')
        self.health=Server(('127.0.0.1',0),reg,'a'*64)
        threading.Thread(target=self.health.serve_forever,daemon=True).start()
        self.addCleanup(self.health.server_close); self.addCleanup(self.health.shutdown)
        self.calls=self.work/'calls.txt'; self.marker=self.work/'reload-failed'

    def execute(self, *, apply=False, fail=False, occupied=False, external_change=False):
        mock='''
function Assert-TunnelAdministrator {}
function Get-TunnelTask { param($Name,$Exe,$ConfigPath) [pscustomobject]@{TaskName=$Name} }
function Get-TunnelCaddyContext { param($CaddyExe,$CaddyConfig) [pscustomobject]@{Exe=$CaddyExe;Config=$CaddyConfig;ReloadArguments='reload mock'} }
function Get-TunnelListeners { param($Ports) if (OCCUPIED -and $Ports -contains 18081) { return ,@([pscustomobject]@{LocalPort=18081}) }; return ,@() }
function Get-ScheduledTask { param($TaskName) }
function Register-TunnelTask { param($Name,$Exe,$Arguments,$ConfigPath,$Directory) Add-Content -LiteralPath CALLS -Value 'register' }
function Start-ScheduledTask { param($TaskName,$InputObject) Add-Content -LiteralPath CALLS -Value 'start' }
function Stop-TunnelTask { param($Name,$Exe,$ConfigPath) Add-Content -LiteralPath CALLS -Value ('stop:'+ $Name) }
function Unregister-ScheduledTask { param($TaskName,$Confirm) Add-Content -LiteralPath CALLS -Value 'unregister' }
function Wait-TunnelListener { param($Port,$Exe,$TimeoutSeconds) if ($Port -eq 7000 -and CHANGE) { [IO.File]::AppendAllText(CONFIG,"`n# 外部变更") } }
function Get-TunnelServerProcesses { param($Exe,$ConfigPath) [pscustomobject]@{ProcessId=11} }
function Invoke-TunnelNative {
 param($Exe,$Arguments,$Directory)
 Add-Content -LiteralPath CALLS -Value $Arguments
 if ($Arguments -like 'reload*' -and FAIL -and -not (Test-Path -LiteralPath MARKER)) {
   [IO.File]::WriteAllText(MARKER,'failed'); throw '模拟 Caddy 重载失败'
 }
}
'''.replace('CALLS',psquote(self.calls)).replace('MARKER',psquote(self.marker)).replace('CONFIG',psquote(self.caddy)).replace('CHANGE','$true' if external_change else '$false').replace('OCCUPIED','$true' if occupied else '$false').replace('FAIL','$true' if fail else '$false')
        helper=self.work/'common/Tunnel.Server.ps1'
        helper.write_text((ROOT/'common/Tunnel.Server.ps1').read_text('utf-8-sig')+'\n'+mock, encoding='utf-8-sig')
        helper=self.work/'common/Tunnel.Collaboration.ps1'
        helper.write_text((ROOT/'common/Tunnel.Collaboration.ps1').read_text('utf-8-sig')+'\nfunction Open-TunnelLegacyGuard { param($Port) return $null }\n', encoding='utf-8-sig')
        command=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(self.work/'server/enable-collaboration.ps1'),
                 '-PythonExe',sys.executable,'-CaddyExe',str(ROOT/'client/caddy.exe'),'-CaddyConfig',str(self.caddy),'-RegistryPort',str(self.health.server_port)]
        if apply: command.append('-Apply')
        return subprocess.run(command,capture_output=True,timeout=30)

    def test_preview_and_occupied_legacy_do_not_change_live_configs(self):
        preview=self.execute(); self.assertEqual(preview.returncode,0,preview.stderr.decode('utf-8','replace'))
        for path, original in self.original.items(): self.assertEqual(path.read_bytes(),original)
        rejected=self.execute(apply=True,occupied=True); self.assertNotEqual(rejected.returncode,0)
        for path, original in self.original.items(): self.assertEqual(path.read_bytes(),original)
        self.assertNotIn('register',self.calls.read_text(encoding='utf-8-sig') if self.calls.exists() else '')

    def test_reload_failure_restores_both_files_and_removes_owned_task(self):
        result=self.execute(apply=True,fail=True)
        self.assertNotEqual(result.returncode,0)
        for path, original in self.original.items(): self.assertEqual(path.read_bytes(),original)
        calls=self.calls.read_text(encoding='utf-8-sig')
        self.assertIn('register',calls); self.assertIn('unregister',calls)

    def test_external_caddy_change_is_not_overwritten(self):
        result=self.execute(apply=True,external_change=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.frps.read_bytes(),self.original[self.frps])
        self.assertTrue(self.caddy.read_text(encoding='utf-8').endswith('# 外部变更'))
        self.assertIn('unregister',self.calls.read_text(encoding='utf-8-sig'))

    def test_apply_preserves_existing_and_adds_registry(self):
        result=self.execute(apply=True)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        self.assertIn('{ start = 30000, end = 30255 }',self.frps.read_text(encoding='utf-8'))
        config=self.caddy.read_text(encoding='utf-8')
        self.assertIn('respond "root" 200',config)
        self.assertIn('reverse_proxy 127.0.0.1:18080',config)
        self.assertIn('forward_auth 127.0.0.1:'+str(self.health.server_port),config)

    def test_manual_start_preserves_coordination_port_configuration(self):
        value=self.frps.read_text(encoding='utf-8')+'auth.method = "token"\nauth.tokenSource.type = "file"\n'
        value=value.replace('allowPorts = [{ single = 18080 }, { single = 18081 }]',
                            'allowPorts = [{ single = 18080 }, { single = 18081 }, { start = 30000, end = 30255 }]')
        self.frps.write_text(value,encoding='utf-8')
        mock='''
function Assert-TunnelAdministrator {}
function Assert-TunnelToken { param($Path) }
function Get-TunnelTask { param($Name,$Exe,$ConfigPath) if ($Name -eq 'PersonalWebTunnel-FRPS') { [pscustomobject]@{TaskName=$Name} } }
function Get-TunnelServerProcesses { param($Exe,$ConfigPath) return @() }
function Get-TunnelListeners { param($Ports) return ,@() }
function Invoke-TunnelNative { param($Exe,$Arguments,$Directory) }
function Start-ScheduledTask { param($InputObject) }
function Wait-TunnelListener { param($Port,$Exe,$TimeoutSeconds) }
'''
        helper=self.work/'common/Tunnel.Server.ps1'
        helper.write_text((ROOT/'common/Tunnel.Server.ps1').read_text('utf-8-sig')+'\n'+mock,encoding='utf-8-sig')
        command=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(self.work/'server/start-server.ps1')]
        result=subprocess.run(command,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        self.assertEqual(self.frps.read_text(encoding='utf-8'),value)
        unsafe=value.replace('proxyBindAddr = "127.0.0.1"','proxyBindAddr = "0.0.0.0"')
        self.frps.write_text(unsafe,encoding='utf-8')
        rejected=subprocess.run(command,capture_output=True,timeout=20)
        self.assertNotEqual(rejected.returncode,0)
        self.assertEqual(self.frps.read_text(encoding='utf-8'),unsafe)

    def test_status_shows_collaboration_boot_task(self):
        mock='''
function Assert-TunnelAdministrator {}
function Get-TunnelTask { param($Name,$Exe,$ConfigPath) return $null }
function Get-ScheduledTask {
 param($TaskName)
 [pscustomobject]@{State='Running'; Actions=@([pscustomobject]@{Execute='python.exe';Arguments=('"'+SCRIPT+'" --port 18083')});
  Triggers=@([pscustomobject]@{CimClass=[pscustomobject]@{CimClassName='MSFT_TaskBootTrigger'}});
  Principal=[pscustomobject]@{UserId='SYSTEM'}}
}
function Get-ScheduledTaskInfo { process { [pscustomobject]@{LastRunTime='now';LastTaskResult=267009} } }
function Get-TunnelServerProcesses { param($Exe,$ConfigPath) return @() }
function Get-TunnelListeners { param($Ports) return ,@() }
function Format-TunnelListeners { param($Listeners) return '无' }
function Test-TunnelHttp { param($Url,$TimeoutSeconds) [pscustomobject]@{Detail='HTTP 200'} }
'''.replace('SCRIPT',psquote(self.work/'server/collaboration_registry.py'))
        helper=self.work/'common/Tunnel.Server.ps1'
        helper.write_text((ROOT/'common/Tunnel.Server.ps1').read_text('utf-8-sig')+'\n'+mock,encoding='utf-8-sig')
        result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(self.work/'server/status-collaboration.ps1')],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode('gbk','replace'))
        output=result.stdout.decode('gbk','replace')
        self.assertIn('Running',output)
        self.assertIn('SYSTEM',output)



if __name__ == '__main__': unittest.main()
