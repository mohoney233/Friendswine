"""Fixed-purpose local launcher and controls for the Identity V visual mod."""
import argparse,csv,ctypes,datetime,hashlib,json,math,os,shutil,subprocess,sys,time,uuid
sys.dont_write_bytecode=True
from ctypes import wintypes
from pathlib import Path
from release_paths import (VERSION,ENGINE_SHA,state_directory,installation_key,
                           validate_game_path,load_configuration,select_game,
                           normalized_path,process_image,sha256_file)
import compatibility as compat

ROOT=Path(__file__).resolve().parent
DATA=state_directory(ROOT)
CHANNEL=DATA/'persistent'
CONFIG=DATA/'config.json'
INJECTOR=ROOT.parent/'bin'/'fuxiang-inject.exe'
EXPORTS=ROOT/'resources'/'neox_exports.root.verified.txt'
EXPORTS_SHA='7c53fd45d952293f4246ff1b2bc61e48aa8e5dc552722c265beb53d7d2d78d99'
BUNDLED_PROFILES=ROOT/'resources'/'compatibility'
PROFILE_STORE=DATA/'compatibility'/'profiles'
PROFILE_PREFERENCES=DATA/'compatibility'/'preferences.json'
COMPAT_CACHE=DATA/'compatibility'/'cache'
CHANNEL_STATE_FILES=('status.json','control.json','watch-control.json','watcher.json')


class RetryBackoff:
    """One bounded retry series per actual game process; no engine access."""
    def __init__(self, max_failures=6, base_delay=30., max_delay=600.):
        self.max_failures=max_failures
        self.base_delay=base_delay
        self.max_delay=max_delay
        self.reset(None)

    def reset(self,key):
        self.key=key
        self.failures=0
        self.next_attempt=0.
        self.suspended=False

    def ready(self,key,now):
        if key!=self.key:
            self.reset(key)
        return not self.suspended and now>=self.next_attempt

    def failed(self,now):
        self.failures+=1
        self.suspended=self.failures>=self.max_failures
        delay=min(self.base_delay*2**(self.failures-1),self.max_delay)
        self.next_attempt=now+delay
        return None if self.suspended else delay


def dirty_status(status):
    core=status.get('controller',{})
    core=core if isinstance(core,dict) else {}
    return {'modified':status.get('modified',core.get('modified',0)),
            'unresolved':core.get('unresolved',0),
            'pending_restores':core.get('pending_restores',0)}


def has_dirty(status):
    return any(dirty_status(status).values())


def heartbeat_info(status,now=None):
    stamp=status.get('heartbeat_at')
    if type(stamp) not in (int,float) or not math.isfinite(stamp):
        return {'heartbeat_age_s':None,'stale':True}
    age=(time.time() if now is None else now)-stamp
    return {'heartbeat_age_s':age,'stale':age>5. or age<0.}


def bootstrap_source(payload,options):
    namespace={'__name__':'friendswine_runtime','__file__':str(payload),'__fw_options__':options}
    return ('exec(compile(open('+repr(str(payload))+',"rb").read(),'+repr(str(payload))+
            ',"exec"),'+repr(namespace)+')\n')


def watch_mutex_name(app_root=None):
    return 'Local\\FriendswineVisualModWatcher_'+installation_key(app_root or ROOT)


def injection_environment(game_dir,tool_root,native=None,pid=None,created=None):
    env=dict(os.environ,FUXIANG_GAME_DIR=str(game_dir),FUXIANG_TOOL_ROOT=str(tool_root))
    if native is not None:
        env.update(FUXIANG_EXPECTED_ENGINE_SHA=native['engine_sha256'],
                   FUXIANG_EXPECTED_NATIVE_FINGERPRINT=native['native_fingerprint'],
                   FUXIANG_EXPECTED_PID=str(pid))
        if created is not None:
            env['FUXIANG_EXPECTED_CREATED_TIME']=format(created,'.7f')
    return env

def inspect_engine(engine):
    """Offline inspection: no game process access, no generated address whitelist."""
    result=subprocess.run([str(INJECTOR),'inspect-engine',str(engine)],
        capture_output=True,text=True,encoding='utf8',timeout=45,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('NATIVE_INSPECTION_FAILED: '+result.stderr[-2000:])
    native=json.loads(result.stdout)
    if (native.get('architecture')!='x64' or
        native.get('engine_sha256')!=sha256_file(engine)):
        raise RuntimeError('ENGINE_CHANGED_DURING_INSPECTION')
    return native

def available_profiles():
    return compat.load_profiles(BUNDLED_PROFILES,PROFILE_STORE)

def preferred_profile():
    return read_json(PROFILE_PREFERENCES).get('profile_id')

def native_candidates(native):
    candidates=compat.matching_profiles(native,available_profiles())
    selected=preferred_profile()
    if selected:
        candidates=[p for p in candidates if p['id']==selected]
    if not candidates:
        raise RuntimeError('UNSUPPORTED_NATIVE_IMPLEMENTATION: 原生实现没有匹配的已验证适配配置；请在控制面板点「错误修复」重新检查，或导入维护者提供的适配包')
    return candidates

def bound_process(pid,created,game):
    if (game_pids()!=[pid] or abs(created_time(pid)-created)>.001 or
        normalized_path(process_image(pid))!=normalized_path(game)):
        raise RuntimeError('PROCESS_CHANGED_BEFORE_ATTACH')

def probe_runtime(game,pid,created,native,profiles,evidence):
    """Read-only script inspection after a matching native implementation gate."""
    nonce=uuid.uuid4().hex
    probe_dir=DATA/'evidence'/nonce
    probe_dir.mkdir(parents=True,exist_ok=False)
    output=probe_dir/'runtime-probe.json'
    options={'pid':pid,'nonce':nonce,'app_root':str(ROOT),'output_file':str(output),
             'profiles':profiles}
    payload=ROOT/'runtime_probe.py'
    namespace={'__name__':'friendswine_readonly_probe','__file__':str(payload),'__fw_probe_options__':options}
    bootstrap=evidence/('probe-bootstrap-'+nonce+'.py')
    bootstrap.write_text('exec(compile(open('+repr(str(payload))+',"rb").read(),'+repr(str(payload))+',"exec"),'+repr(namespace)+')\n',encoding='utf8')
    bound_process(pid,created,game)
    result=subprocess.run([str(INJECTOR),'raw',str(bootstrap)],
        env=injection_environment(game.parent,evidence/'toolroot',native,pid,created),
        capture_output=True,timeout=135,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('READONLY_PROBE_DISPATCH_FAILED: '+result.stderr.decode('utf8',errors='replace')[-2000:])
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        value=read_json(output)
        if value.get('nonce')==nonce and value.get('pid')==pid and value.get('done') is True:
            if value.get('error'):
                raise RuntimeError('READONLY_PROBE_FAILED: '+str(value['error']))
            compat.validate_probe_result(value,nonce,pid,profiles)
            bound_process(pid,created,game)
            return value
        time.sleep(.1)
    raise RuntimeError('READONLY_PROBE_TIMEOUT: 未收到本次主线程采样结果')

def choose_runtime_profile(profiles,probe,battle_enabled=False):
    records={item['id']:item for item in probe.get('profiles',[])}
    matches=[]
    for profile in profiles:
        record=records.get(profile['id'],{})
        caps=record.get('diagnostics',{}).get('capabilities',{})
        names=['display','training'] if battle_enabled else ['display']
        states=[caps.get(name,{}).get('status') for name in names]
        # Deferred modules are checked again before the first model write.
        if 'SUPPORTED' in states or (states and all(s=='WAITING_FOR_SCENE' for s in states)):
            matches.append(profile)
    if len(matches)>1:
        raise RuntimeError('AMBIGUOUS_RUNTIME_PROFILE: 请使用 profile-select --profile 指定已验证配置')
    if not matches:
        raise RuntimeError('SCRIPT_IMPLEMENTATION_CHANGED: 没有通过当前脚本检查的能力；保留探针报告，未启动缩放')
    return matches[0]

def cache_probe(native,profile,probe):
    record=next((item for item in probe.get('profiles',[]) if item['id']==profile['id']),{})
    observed=record.get('observed_runtime',{})
    fingerprint=hashlib.sha256(compat.canonical_bytes(observed)).hexdigest()
    return compat.save_cache(COMPAT_CACHE,native['engine_sha256'],native['native_fingerprint'],
                             fingerprint,profile,{'scope':'read_only_interface_check','diagnostics':record,
                                                 'model_behavior_validated':False})

def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.'+str(os.getpid())+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
    os.replace(temp,path)

def read_json(path):
    # The runtime publishes status atomically and retries Windows sharing
    # violations; the reader needs the same tolerance. A momentary lock must
    # never turn a successful action into a reported failure.
    deadline=time.monotonic()+.6
    while True:
        try:
            value=json.loads(path.read_text(encoding='utf8'))
            return value if isinstance(value,dict) else {}
        except (FileNotFoundError,json.JSONDecodeError):
            return {}
        except OSError as error:
            if getattr(error,'winerror',None) not in (5,32,33) or time.monotonic()>=deadline:
                return {}
            time.sleep(.05)

def game_pids():
    rows=csv.reader(subprocess.check_output(['tasklist','/FI','IMAGENAME eq dwrg.exe','/FO','CSV','/NH'],text=True,creationflags=subprocess.CREATE_NO_WINDOW).splitlines())
    return [int(row[1]) for row in rows if len(row)>1 and row[0].lower()=='dwrg.exe']

def created_time(pid):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,pid)
    try:
        stamps=[wintypes.FILETIME() for _ in range(4)]
        if not handle or not kernel.GetProcessTimes(handle,*[ctypes.byref(stamp) for stamp in stamps]):
            raise RuntimeError('无法验证游戏进程创建时间')
        return ((stamps[0].dwHighDateTime<<32)+stamps[0].dwLowDateTime)/10000000-11644473600
    finally:
        if handle:
            kernel.CloseHandle(handle)

def live_status():
    status=read_json(CHANNEL/'status.json')
    if not status or status.get('pid') not in game_pids():
        return {}
    try:
        if abs(created_time(status['pid'])-status.get('game_created_unix',0))>.001:
            return {}
    except (OSError,RuntimeError,TypeError):
        return {}
    return status

def command(mode,expected_run_id=None):
    status=live_status()
    if expected_run_id is not None and status.get('run_id')!=expected_run_id:
        return {'requested':False,'reason':'会话已变化；未控制其他会话'}
    if status.get('done') and has_dirty(status):
        return {'requested':False,'phase':status.get('phase'),'restored':False,
                **dirty_status(status),'reason':'会话已停止但仍有未解决恢复；保留现场，不能宣称恢复完成'}
    if not status or status.get('done'):
        return {'requested':False,'reason':'当前没有运行中的模组'}
    sequence=str(uuid.uuid4())
    atomic_json(CHANNEL/'control.json',{'run_id':status['run_id'],'mode':mode,'sequence':sequence})
    deadline=time.monotonic()+7
    while time.monotonic()<deadline:
        current=read_json(CHANNEL/'status.json')
        if current.get('run_id')==status['run_id'] and current.get('command_sequence')==sequence:
            if ((mode=='STOP' and current.get('done')) or
                (mode=='PAUSED' and current.get('phase')=='PAUSED' and
                 (not has_dirty(current) or current.get('controller',{}).get('unresolved',0)>0)) or
                (mode=='RUNNING' and current.get('phase') in ('RUNNING','WAITING_FOR_MODELS'))):
                output={'requested':True,'acknowledged':True,'phase':current.get('phase'),
                        **dirty_status(current),'run_id':current['run_id']}
                if mode in ('STOP','PAUSED'):
                    output['restored']=current.get('phase') in ('STOPPED','PAUSED') and not has_dirty(current)
                return output
        time.sleep(.1)
    return {'requested':True,'acknowledged':False,'reason':'等待游戏主线程执行控制；未宣称恢复完成'}

def watch_is_current(generation):
    control=read_json(CHANNEL/'watch-control.json')
    return control.get('enabled') is True and control.get('generation')==generation


def start(battle_enabled=False,watch_generation=None):
    if type(battle_enabled) is not bool:
        raise ValueError('battle_enabled 必须为 bool')
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('请从已用管理员身份运行的终端或 Codex 启动；本工具不修改 UAC 设置')
    pids=game_pids()
    GAME=select_game(load_configuration(CONFIG),pids)
    ENGINE=GAME.parent/'neox_engine.dll'
    pid=pids[0]
    live=live_status()
    if live and not live.get('done'):
        if heartbeat_info(live)['stale']:
            raise RuntimeError('存在存活游戏会话但状态心跳已过期或缺失；需要核实状态，未盲目重复接入')
        if live.get('version')!=VERSION:
            raise RuntimeError('已有其他版本运行；先停止恢复后再启动')
        if has_dirty(live) and live.get('controller',{}).get('unresolved'):
            raise RuntimeError('运行会话有未解决尺度争用；先停止并核实恢复')
        live_battle=live.get('battle_enabled')
        if live_battle is None:
            live_battle=any(item.get('kind')=='battle' for item in live.get('targets',[]) if isinstance(item,dict))
        if bool(live_battle)!=battle_enabled:
            raise RuntimeError('当前会话的启用范围与本次请求不同（例如先前的会话说未启用对局适配）；请先在面板点「停止」，再点「启动」')
        return {'already_running':True,'phase':live.get('phase'),'run_id':live.get('run_id'),'pid':pid}
    if live and has_dirty(live):
        raise RuntimeError('前一会话存在未恢复模型的残留记录；保留现场，未重复接入。'
                           '请在控制面板点「错误修复」清理残留状态后再启动；若仍失败请重启游戏')
    run_id='persistent-'+uuid.uuid4().hex
    if not INJECTOR.is_file():
        raise RuntimeError('安装包缺少内部接入器；请重新安装')
    CHANNEL.mkdir(parents=True,exist_ok=True)
    evidence=DATA/'evidence'/'persistent-runs'/run_id
    evidence.mkdir(parents=True,exist_ok=True)
    report={'run_id':run_id,'pid':pid,'started_at':time.time(),'battle_enabled':battle_enabled}
    try:
        checked=subprocess.run([sys.executable,str(ROOT/'probe_environment.py'),'--pid',str(pid),'--out',str(evidence/'preflight.json')],capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
        if checked.returncode:
            raise RuntimeError('进程诊断失败 '+checked.stderr)
        diagnostic=read_json(evidence/'preflight.json')
        modules=[entry for entry in diagnostic.get('engine_modules',[]) if entry['name'].lower()=='neox_engine.dll']
        if (not diagnostic.get('observer_token',{}).get('elevated') or
            normalized_path(diagnostic.get('target_image',''))!=normalized_path(GAME)):
            raise RuntimeError('权限或目标路径校验失败')
        if len(modules)!=1 or normalized_path(modules[0]['path'])!=normalized_path(ENGINE):
            raise RuntimeError('当前引擎加载路径不匹配')
        native=inspect_engine(ENGINE)
        report['native']=native
        if modules[0]['disk_sha256']!=native['engine_sha256']:
            raise RuntimeError('引擎文件在诊断期间发生变化')
        candidates=native_candidates(native)
        created=created_time(pid)
        report['game_created_unix']=created
        probe=probe_runtime(GAME,pid,created,native,candidates,evidence)
        report['readonly_probe']=probe
        profile=choose_runtime_profile(candidates,probe,battle_enabled)
        report['profile_id']=profile['id']
        cache_probe(native,profile,probe)
        toolroot=CHANNEL/'toolroot'
        payload=ROOT/'persistent_runtime.py'
        options={'run_id':run_id,'pid':pid,'game_created_unix':created,'battle_enabled':battle_enabled,
                 'app_root':str(ROOT),'channel':str(CHANNEL),
                 'compatibility_profile':dict(profile,profile_id=profile['id'])}
        bootstrap=evidence/'bootstrap.py'
        bootstrap.write_text(bootstrap_source(payload,options),encoding='utf8')
        report['sha256']={name:sha256_file(ROOT/name) for name in ('persistent_runtime.py','persistent_core.py','persistent_adapter.py','training_core.py','release_paths.py','runtime_compat.py','runtime_probe.py','compatibility.py')}
        if (game_pids()!=[pid] or abs(created_time(pid)-created)>.001 or
            normalized_path(process_image(pid))!=normalized_path(GAME)):
            raise RuntimeError('接入前游戏进程身份发生变化')
        if watch_generation is not None and not watch_is_current(watch_generation):
            raise RuntimeError('自动重连已停止；未接入游戏')
        env=injection_environment(GAME.parent,toolroot,native,pid,created)
        result=subprocess.run([str(INJECTOR),'raw',str(bootstrap)],env=env,capture_output=True,timeout=135,creationflags=subprocess.CREATE_NO_WINDOW)
        report.update(injector_exit=result.returncode,stdout=result.stdout.decode('utf8',errors='replace'),stderr=result.stderr.decode('utf8',errors='replace'))
        if result.returncode:
            raise RuntimeError('RUNTIME_DISPATCH_FAILED: '+report['stderr'][-2000:])
        # One rendered frame is the main-thread receipt that proves the injected
        # bootstrap actually ran; the frame counter may advance slowly while the
        # game window is unfocused or throttled, so allow a bounded longer window
        # instead of rejecting a real session after a few seconds.
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            status=read_json(CHANNEL/'status.json')
            if status.get('run_id')==run_id and status.get('frames',0)>=1:
                report['runtime']=status
                if status.get('done'):
                    raise RuntimeError('游戏内启动失败：'+str(status.get('error')))
                if watch_generation is not None and not watch_is_current(watch_generation):
                    return {'started':False,'watch_cancelled':True,
                            'stop':command('STOP',expected_run_id=run_id)}
                return {'started':True,'run_id':run_id,'pid':pid,'phase':status.get('phase'),
                        'targets':status.get('controller',{}).get('bound'),'duration_limit':None,
                        'battle_enabled':battle_enabled,'profile_id':profile['id'],
                        'compatibility':status.get('compatibility')}
            if status.get('run_id')==run_id and status.get('done'):
                raise RuntimeError('游戏内启动失败：'+str(status.get('error')))
            time.sleep(.1)
        raise RuntimeError('未收到本轮游戏主线程回执；不能将注入器退出码当成启动成功')
    except BaseException as error:
        report['error']=str(error)
        raise
    finally:
        report['finished_at']=time.time()
        atomic_json(evidence/'host.json',report)

def set_watch(enabled,battle_enabled=False):
    generation=uuid.uuid4().hex
    atomic_json(CHANNEL/'watch-control.json',{'enabled':bool(enabled),'at':time.time(),
                'battle_enabled':bool(battle_enabled),'generation':generation})
    return generation

def watch():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateMutexW(None,False,watch_mutex_name())
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error()==183:
        kernel.CloseHandle(handle)
        return
    observer={'pid':os.getpid(),'created_at':time.time(),'state':'WATCHING','events':[]}
    attempted=None
    generation=None
    backoff=RetryBackoff()
    try:
        while True:
            control=read_json(CHANNEL/'watch-control.json')
            if control.get('enabled') is not True:
                break
            if control.get('generation')!=generation:
                generation=control.get('generation')
                attempted=None
                backoff.reset(None)
            try:
                pids=game_pids()
                if len(pids)==1:
                    key=(pids[0],created_time(pids[0]))
                    if key!=attempted and backoff.ready(key,time.monotonic()):
                        try:
                            result=start(battle_enabled=control.get('battle_enabled') is True,
                                         watch_generation=generation)
                            attempted=key
                            backoff.reset(key)
                            observer.pop('last_error',None)
                            observer['events'].append({'at':time.time(),'result':result})
                        except Exception as error:
                            delay=backoff.failed(time.monotonic())
                            observer['last_error']=str(error)[:500]
                            observer['events'].append({'at':time.time(),'error':str(error)[:500],
                                                      'retry_in_s':delay,'suspended':backoff.suspended})
                elif not pids:
                    attempted=None
                    backoff.reset(None)
            except Exception as error:
                observer['last_error']=str(error)[:500]
            observer['retry']={'process':list(backoff.key) if backoff.key else None,
                               'failures':backoff.failures,'suspended':backoff.suspended,
                               'next_attempt_at':time.time()+max(0.,backoff.next_attempt-time.monotonic())
                                    if backoff.failures and not backoff.suspended else None}
            observer['battle_enabled']=control.get('battle_enabled') is True
            observer.update(heartbeat_at=time.time(),events=observer['events'][-24:])
            atomic_json(CHANNEL/'watcher.json',observer)
            time.sleep(2)
    finally:
        observer.update(state='STOPPED',heartbeat_at=time.time())
        atomic_json(CHANNEL/'watcher.json',observer)
        kernel.CloseHandle(handle)

def start_watch(battle_enabled=False):
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('请用管理员身份启动自动重连')
    set_watch(True,battle_enabled)
    existing=read_json(CHANNEL/'watcher.json')
    if existing.get('state')=='WATCHING' and time.time()-existing.get('heartbeat_at',0)<5:
        return {'watcher':'already_running','retry_reset':True,'battle_enabled':battle_enabled}
    pythonw=Path(sys.executable).with_name('pythonw.exe')
    subprocess.Popen([str(pythonw if pythonw.exists() else Path(sys.executable)),str(Path(__file__).resolve()),'watch'],cwd=str(ROOT),creationflags=subprocess.CREATE_NO_WINDOW,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return {'watcher':'started','auto_reconnect':True,'battle_enabled':battle_enabled,
            'max_failures':6,'retry_delays_s':[30,60,120,240,480]}


def cleanup():
    """Archive a dead session's stale channel state so it cannot block a start.

    Only files this tool wrote are touched, and every one is copied into
    evidence before removal. A live session is never disturbed, and this never
    claims that any model was restored: restoration is only ever performed by
    停止 inside the game's main thread.
    """
    status=live_status()
    if status and not status.get('done') and not heartbeat_info(status)['stale']:
        raise RuntimeError('当前仍有运行中的会话；请先在控制面板点「停止」，再清理残留状态')
    if (CHANNEL/'watcher.json').is_file():
        set_watch(False)
    # Collect first: an empty cleanup must not leave an empty evidence folder.
    sources=[(CHANNEL/name,name) for name in CHANNEL_STATE_FILES if (CHANNEL/name).is_file()]
    sources+=[(path,path.name) for path in sorted(CHANNEL.glob('rejected-*.json'))]
    if not sources:
        return {'cleaned':True,'archived':[],'archived_to':None,
                'live_session':bool(status) and not status.get('done'),
                'note':'没有需要清理的残留状态'}
    target=DATA/'evidence'/('cleanup-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    target.mkdir(parents=True,exist_ok=True)
    archived=[]
    for source,name in sources:
        shutil.copy2(source,target/name)
        source.unlink()
        archived.append(name)
    return {'cleaned':True,'archived':archived,'archived_to':str(target),
            'live_session':bool(status) and not status.get('done'),
            'note':'已归档并清除上一次会话的通道状态；未修改游戏，也不代表模型已恢复'}


def configure(game_path):
    game=validate_game_path(game_path)
    atomic_json(CONFIG,{'schema':1,'game_exe':str(game)})
    return {'configured':True,'version':VERSION,'game_exe':str(game),'config':str(CONFIG),
            'compatibility_check_required':True}


def doctor():
    """Read-only files/process metadata; never initialize a live control channel."""
    result={'version':VERSION,'app_root':str(ROOT),'state_dir':str(DATA),
            'config':str(CONFIG),'python':sys.executable,
            'injector_present':INJECTOR.is_file(),
            'elevated':bool(ctypes.windll.shell32.IsUserAnAdmin()),'ready_to_start':False,
            'native_supported':False,'runtime_check_required':True,
            'scope':'costume display; --battle enables the verified battle adaptation in any battle world'}
    try:
        config=load_configuration(CONFIG)
        result['configured_game']=config.get('game_exe')
        pids=game_pids()
        result['game_pids']=pids
        game=select_game(config,pids)
        result['game_exe']=str(game)
        checked=subprocess.run([sys.executable,str(ROOT/'probe_environment.py'),'--pid',str(pids[0])],
                               capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        if checked.returncode:
            raise RuntimeError('只读进程诊断失败 '+checked.stderr[:1000])
        diagnostic=json.loads(checked.stdout)
        result['process']=diagnostic
        modules=[entry for entry in diagnostic.get('engine_modules',[]) if entry.get('name','').lower()=='neox_engine.dll']
        engine=game.parent/'neox_engine.dll'
        native=inspect_engine(engine)
        result['native']=native
        profiles=available_profiles()
        matches=compat.matching_profiles(native,profiles)
        result['matching_profiles']=[{'id':p['id'],'origin':p['_origin'],'revision':p['revision']} for p in matches]
        result['preferred_profile']=preferred_profile()
        result['loaded_path_matches']=(len(modules)==1 and
            normalized_path(modules[0]['path'])==normalized_path(engine) and
            modules[0].get('disk_sha256')==native['engine_sha256'])
        result['native_supported']=bool(matches and result['loaded_path_matches'])
        # Native readiness is not a claim about uninspected live scripts/models.
        result['ready_for_runtime_probe']=bool(result['elevated'] and result['native_supported'] and result['injector_present'])
        result['reason']='RUNTIME_PROBE_REQUIRED' if matches else 'UNSUPPORTED_NATIVE_IMPLEMENTATION'
    except Exception as error:
        result['diagnostic_error']=str(error)
    return result

def check_update(live=False,battle_enabled=False):
    result=doctor()
    result['action']='check-update'
    result['model_writes']=False
    if not live:
        return result
    if not result.get('ready_for_runtime_probe'):
        raise RuntimeError('无法进行运行时检查: '+json.dumps(result,ensure_ascii=False))
    game=Path(result['game_exe'])
    pid=result['game_pids'][0]
    created=created_time(pid)
    native=result['native']
    profiles=native_candidates(native)
    evidence=DATA/'evidence'/'compatibility-checks'/uuid.uuid4().hex
    evidence.mkdir(parents=True,exist_ok=False)
    probe=probe_runtime(game,pid,created,native,profiles,evidence)
    result['runtime_probe']=probe
    result['report']=str(evidence/'compatibility.json')
    try:
        profile=choose_runtime_profile(profiles,probe,battle_enabled)
        result['profile_id']=profile['id']
        record=next(item for item in probe['profiles'] if item['id']==profile['id'])
        capabilities=record['diagnostics']['capabilities']
        requested=['display','training'] if battle_enabled else ['display']
        states={name:capabilities[name]['status'] for name in requested}
        result['capability_states']=states
        result['script_check_passed']=any(s=='SUPPORTED' for s in states.values())
        result['pending']=any(s=='WAITING_FOR_SCENE' for s in states.values())
        result['partial']=result['script_check_passed'] and any(s!='SUPPORTED' for s in states.values())
        result['cache']=cache_probe(native,profile,probe)
    except RuntimeError as error:
        result['script_check_passed']=False
        result['reason']=str(error)
    atomic_json(evidence/'compatibility.json',result)
    return result

def profile_action(action,filename=None,profile_id=None):
    if action=='profiles':
        return {'preferred_profile':preferred_profile(),'profiles':[
            {'id':p['id'],'revision':p['revision'],'origin':p['_origin'],
             'native_fingerprint':p['native_fingerprint'],'source':p['source']}
            for p in available_profiles()]}
    if action=='profile-import':
        if not filename:
            filename=input('适配包 JSON/ZIP 的完整路径: ').strip().strip('"')
        result=compat.import_profile_pack(Path(filename),PROFILE_STORE,BUNDLED_PROFILES)
        if len(result['ids'])==1:
            atomic_json(PROFILE_PREFERENCES,{'profile_id':result['ids'][0]})
            result['selected']=result['ids'][0]
        return result
    if action=='profile-select':
        if profile_id!='auto' and profile_id not in [p['id'] for p in available_profiles()]:
            raise ValueError('未知适配配置；先用 profiles 查看')
        atomic_json(PROFILE_PREFERENCES,{'profile_id':None if profile_id=='auto' else profile_id})
        return {'selected':preferred_profile()}
    if action=='profile-remove':
        result=compat.remove_imported_profile(profile_id,PROFILE_STORE,BUNDLED_PROFILES)
        if preferred_profile()==profile_id:
            atomic_json(PROFILE_PREFERENCES,{'profile_id':None})
        return result
    if action=='cache-clear':
        return compat.clear_cache(COMPAT_CACHE)
    raise ValueError('未知配置操作')

def activity_verdict(action,output):
    """One plain-language Chinese line appended after the JSON report."""
    if not isinstance(output,dict):
        return None
    error=output.get('diagnostic_error')
    if action in ('doctor','check-update'):
        if error:
            return '发现问题：'+str(error)
        if not output.get('injector_present'):
            return '发现问题：缺少内部接入器，请重新安装后再试'
        if not output.get('elevated'):
            return '发现问题：当前未以管理员身份运行，启动效果需要管理员权限'
        if not output.get('game_pids'):
            return '发现问题：没有检测到正在运行的 dwrg.exe，请先启动游戏'
        if not output.get('matching_profiles'):
            return '发现问题：没有匹配的已验证适配配置；可在控制面板点「错误修复」重新检查'
        if not output.get('loaded_path_matches'):
            return '发现问题：游戏实际加载的引擎与磁盘文件不一致'
        if action=='check-update':
            return '诊断结果正常：原生接口匹配，可以直接点「启动」'
        return '诊断结果正常：路径、权限、进程与原生接口检查均通过'
    if action=='validate':
        if error:
            return '发现问题：'+str(error)
        if output.get('pending'):
            return '发现问题：所需脚本尚未加载，请进入角色时装展示页或匹配对局后重试'
        if output.get('script_check_passed') and not output.get('partial'):
            return '诊断结果正常：展示与对局所需的脚本接口均已通过只读检查'
        return '发现问题：'+str(output.get('reason') or '脚本检查未通过')
    if action=='cleanup':
        if output.get('archived'):
            return '已归档并清除上一次会话的残留状态，现在可以重新点「启动」'
        return '没有需要清理的残留状态'
    if action=='status':
        phase=output.get('phase')
        running=phase in ('RUNNING','WAITING_FOR_SCENE','WAITING_FOR_MODELS','PAUSED','RESTORING')
        # 运行中的 modified 是"正在生效的模型数"，不是残留；只有会话结束后
        # 仍然存在的改动才需要处理。否则正常运行的每一帧都会被误报成问题。
        if not running and any(output.get(name,0) for name in ('modified','unresolved','pending_restores')):
            return '发现问题：仍有未恢复的残留改动，请点「错误修复」清理残留状态；若仍失败请重启游戏'
        if output.get('unresolved',0):
            return '发现问题：存在无法确认恢复的模型，请在控制面板点「停止」；若停止无效，点「错误修复」'
        if phase in ('RUNNING','WAITING_FOR_MODELS'):
            modified=output.get('modified',0)
            return '诊断结果正常：模组正在运行'+('，正在修改 '+str(modified)+' 个模型' if modified else '')
        if phase=='PAUSED':
            return '诊断结果正常：模组已暂停'
        if phase=='RESTORING':
            return '诊断结果正常：正在恢复模型'
        if phase=='RESTORE_FAULT':
            return '发现问题：恢复过程异常，请点「错误修复」清理残留状态'
        if phase=='WAITING_FOR_SCENE':
            return '诊断结果正常：已接入游戏，正在等待进入角色时装展示页或匹配对局'
        return '诊断结果正常：当前没有运行中的模组'
    return None

def main():
    parser=argparse.ArgumentParser(description='第五人格朋友的酒：持续人物外观缩放')
    parser.add_argument('--version',action='version',version=VERSION)
    parser.add_argument('action',choices=('start','stop','pause','resume','status','watch','start-watch','configure','doctor','check-update','validate','profiles','profile-import','profile-select','profile-remove','cache-clear','cleanup'),
                        help='cleanup: 归档并清除上一次会话的残留状态，之后可以重新启动')
    parser.add_argument('--battle',action='store_true',help='开启本机已验证的对局适配；匹配对局可生效')
    parser.add_argument('--game',help='configure: 存在的 dwrg.exe 绝对路径；省略配置时自动识别唯一运行进程')
    parser.add_argument('--file',help='profile-import: 本地 JSON/ZIP 纯数据适配包')
    parser.add_argument('--profile',help='profile-select/profile-remove: 配置 ID；select 可设 auto')
    args=parser.parse_args()
    if args.file and args.action!='profile-import':
        parser.error('--file 仅用于 profile-import')
    if args.profile and args.action not in ('profile-select','profile-remove'):
        parser.error('--profile 仅用于 profile-select/profile-remove')
    if args.action in ('profile-select','profile-remove') and not args.profile:
        parser.error('需要 --profile ID')
    if args.action=='configure':
        if not args.game:
            parser.error('configure 需要 --game <dwrg.exe绝对路径>')
        output=configure(args.game)
    elif args.game:
        parser.error('--game 仅适用于 configure')
    elif args.action=='doctor':
        output=doctor()
    elif args.action in ('check-update','validate'):
        output=check_update(live=args.action=='validate',battle_enabled=args.battle)
    elif args.action=='cleanup':
        output=cleanup()
    elif args.action in ('profiles','profile-import','profile-select','profile-remove','cache-clear'):
        output=profile_action(args.action,args.file,args.profile)
    elif args.action=='start':
        output=start(battle_enabled=args.battle)
    elif args.action=='start-watch':
        output=start_watch(battle_enabled=args.battle)
    elif args.action=='watch':
        watch()
        return
    elif args.action=='stop':
        set_watch(False)
        output=command('STOP')
    elif args.action in ('pause','resume'):
        output=command('PAUSED' if args.action=='pause' else 'RUNNING')
    else:
        status=live_status()
        watcher=read_json(CHANNEL/'watcher.json')
        output={'version':VERSION,'state_dir':str(DATA),'phase':status.get('phase','NOT_ATTACHED'),'pid':status.get('pid'),'run_id':status.get('run_id'),'frames':status.get('frames'),'targets':status.get('controller',{}).get('bound'),'blocked':status.get('controller',{}).get('blocked'),**dirty_status(status),**heartbeat_info(status),'last_frame_at':status.get('last_frame_at'),'battle_enabled':status.get('battle_enabled'),'compatibility':status.get('compatibility'),'watcher':watcher.get('state'),'watcher_pid':watcher.get('pid'),'watcher_retry':watcher.get('retry'),'watcher_last_error':watcher.get('last_error')}
    print(json.dumps(output,ensure_ascii=False,indent=2))
    # The plain-language verdict goes to stderr so stdout stays valid JSON for
    # callers that parse it (the launcher prints stdout before stderr, so the
    # verdict still appears last on screen).
    summary=activity_verdict(args.action,output)
    if summary:
        print(summary,file=sys.stderr)

if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print(str(error),file=sys.stderr)
        sys.exit(1)
