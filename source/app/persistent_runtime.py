"""Process-resident visual mod: one main-thread callback, fixed JSON controls."""
import os,sys,time,json,types,threading,traceback
from collections import deque

REGISTRY='friendswine_persistent_v1'
VERSION='1.3.0'
options=globals().get('__fw_options__',{})
if type(options) is not dict:
    options={}
app_root=options.get('app_root')
channel_root=options.get('channel')
if (not isinstance(app_root,str) or not os.path.isabs(app_root) or
    not isinstance(channel_root,str) or not os.path.isabs(channel_root)):
    raise RuntimeError('INVALID_PORTABLE_RUNTIME_PATHS')
ROOT=os.path.normpath(app_root)+os.sep
CHANNEL=os.path.normpath(channel_root)+os.sep
if os.path.normcase(os.path.dirname(os.path.abspath(__file__)))!=os.path.normcase(os.path.normpath(app_root)):
    raise RuntimeError('RUNTIME_SOURCE_ROOT_MISMATCH')
if not os.path.isdir(CHANNEL):
    raise RuntimeError('RUNTIME_CHANNEL_MISSING')
OUT=CHANNEL+'status.json'
CONTROL=CHANNEL+'control.json'
run_id=options.get('run_id')
expected_pid=options.get('pid')
lock=threading.RLock()
publish_lock=threading.Lock()
state={'version':VERSION,'run_id':run_id,'pid':os.getpid(),'phase':'STARTING',
       'created_at':time.time(),'done':False,'frames':0,'duration_limit':None,
       'game_created_unix':options.get('game_created_unix'),
       'battle_enabled':options.get('battle_enabled',False),
       'battle_scope':'display page and battle worlds','modified':0,
       'app_root':app_root,'channel':channel_root}
pending={'mode':'RUNNING','sequence':'initial'}
adapter=controller=compatibility=scheduler=None
existing=sys.modules.get(REGISTRY)
snapshot_at=0.
stop_started=None
durations=deque(maxlen=600)
samples=deque(maxlen=24)

class CombinedController:
    """Distinct verified scale semantics for display and local training."""
    def __init__(self,display,training):
        self.display,self.training=display,training
        self.mode='RUNNING'
    @property
    def targets(self):
        return dict(list(self.display.targets.items())+list(self.training.targets.items()))
    def tick(self,handles,now):
        self.display.tick([h for h in handles if h.kind=='display'],now)
        self.training.tick([h for h in handles if h.kind=='battle'],now)
    def pause(self,now):
        self.mode='PAUSED'
        self.display.pause(now)
        self.training.pause(now)
    def resume(self,now):
        self.mode='RUNNING'
        self.display.resume(now)
        self.training.resume(now)
    def stop(self,now=None):
        self.mode='STOPPED'
        result=dict(self.display.stop(now))
        result.update(self.training.stop(now))
        return result
    def clear(self):
        for engine in (self.display,self.training):
            engine.targets.clear()
            engine.pending_restores.clear()
    def status(self):
        display,training=self.display.status(),self.training.status()
        result={'mode':self.mode,'elapsed_s':max(display.get('elapsed_s',0),training.get('elapsed_s',0))}
        for key in ('targets','bound','blocked','modified','unresolved','pending_restores'):
            result[key]=display.get(key,0)+training.get(key,0)
        counts={}
        for status in (display,training):
            for key,value in status.get('counts',{}).items():
                counts[key]=counts.get(key,0)+value
        result['counts']=counts
        result['events']=(display.get('events',[])[-32:]+training.get('events',[])[-32:])
        result['engines']={'display':{k:v for k,v in display.items() if k!='events'},
                           'training':{k:v for k,v in training.items() if k!='events'}}
        return result

def atomic_status_write(output,encoded,*,retry=True,replace=None,sleeper=None,clock=None):
    """Atomic file-only update; retry transient Windows sharing errors <= .6s."""
    replace=os.replace if replace is None else replace
    sleeper=time.sleep if sleeper is None else sleeper
    clock=time.monotonic if clock is None else clock
    output=str(output)
    temp=output+'.'+str(threading.get_ident())+'.'+str(time.time_ns())+'.tmp'
    deadline=clock()+.6
    delays=(.02,.04,.08,.16,.20,.10)
    failure=None
    attempts=0
    try:
        while True:
            if failure is not None and (attempts>=len(delays) or clock()>=deadline):
                raise failure
            try:
                with open(temp,'w',encoding='utf8') as handle:
                    handle.write(encoded)
                replace(temp,output)
                return
            except OSError as error:
                if not retry or getattr(error,'winerror',None) not in (5,32,33):
                    raise
                failure=error
                remaining=deadline-clock()
                if remaining<=0 or attempts>=len(delays):
                    raise
                sleeper(min(delays[attempts],remaining))
                attempts+=1
    finally:
        try:
            os.unlink(temp)
        except OSError:
            # A sharing lock can also prevent cleanup; never mask the original
            # publication error or delete/modify the existing valid status.
            pass


def publish(retry=True,nonblocking=False):
    # The done callback must not wait for a reporter doing bounded file retries.
    if not publish_lock.acquire(blocking=not nonblocking):
        return False
    try:
        with lock:
            snapshot=dict(state)
            snapshot['heartbeat_at']=time.time()
            encoded=json.dumps(snapshot,ensure_ascii=False,indent=2)
        atomic_status_write(OUT,encoded,retry=retry)
        return True
    finally:
        publish_lock.release()

DEAD_SESSION_GRACE=5.

def unresolved(session):
    previous=session.state
    if not previous.get('done'):
        return True
    # A session that already finished no longer runs a callback, so it can
    # never restore anything. Keeping its dirty record latched would block
    # every later start inside the same game process; after a short grace
    # window a new session may attach. It never claims the old one restored.
    finished=previous.get('finished_at')
    if type(finished) in (int,float) and time.time()-finished>DEAD_SESSION_GRACE:
        return False
    return (previous.get('modified') or
            previous.get('controller',{}).get('pending_restores',0)>0 or
            previous.get('controller',{}).get('unresolved',0)>0)

def load_private(filename,name):
    module=types.ModuleType(name)
    module.__file__=ROOT+filename
    with open(module.__file__,'rb') as handle:
        code=compile(handle.read(),module.__file__,'exec')
    exec(code,module.__dict__)
    return module

def sample_targets(handles):
    output=[]
    for handle in handles:
        try:
            item=controller.targets.get(adapter.identity(handle))
            info=adapter.metadata(handle)
            info.update(scale=adapter.read_scale(handle),baseline=item.baseline if item else None,
                        blocked=item.blocked if item else None,reason=item.reason if item else None,
                        unresolved=item.unresolved if item else None,
                        last_written=item.last_written if item else None)
            if handle.kind=='battle':
                info.update(scale_sample_phase='immediate_setter_readback',
                            composed_scale_before_write=getattr(item,'last_observed_scale',None),
                            expected_composed_ratio=getattr(item,'observed_expected_ratio',None),
                            initial_observed_scale=getattr(item,'initial_observed_scale',None))
                fields=vars(handle.owner)
                info['logic_scale']={key:fields.get(key) for key in
                    ('init_scale','last_scale','col_radius','col_height_offset')
                    if type(fields.get(key)) in (int,float,bool,type(None))}
                info['collision_ids']={key:id(fields[key]) if fields.get(key) is not None else None
                                       for key in ('ghost_col','hurt_col')}
            output.append(info)
        except Exception as error:
            output.append({'sample_error':str(error)[:240]})
    return output

def update_snapshot(handles,now,force=False):
    global snapshot_at
    if not force and now-snapshot_at < .5:
        return
    snapshot_at=now
    status=controller.status()
    state['controller']=status
    state['modified']=status['modified']
    state['adapter']=adapter.diagnostics()
    state['compatibility']=compatibility.diagnostics()
    state['targets']=sample_targets(handles)
    state['last_frame_at']=time.time()
    if controller.mode=='PAUSED':
        state['phase']='PAUSED'
    elif status['bound']:
        state['phase']='RUNNING'
    else:
        capabilities=state['compatibility']['capabilities']
        enabled=('display','training') if options.get('battle_enabled') else ('display',)
        states=[capabilities[name]['status'] for name in enabled]
        active=adapter.scene_capability
        active_state=capabilities[active]['status'] if active in enabled else None
        state['phase']=(active_state if active_state in ('IMPLEMENTATION_CHANGED','UNSUPPORTED_INTERFACE')
                        else 'IMPLEMENTATION_CHANGED' if all(value=='IMPLEMENTATION_CHANGED' for value in states)
                        else 'UNSUPPORTED_INTERFACE' if all(value in ('UNSUPPORTED_INTERFACE','IMPLEMENTATION_CHANGED') for value in states)
                        else 'WAITING_FOR_SCENE')
    samples.append({'at':time.time(),'mode':controller.mode,'targets':state['targets']})
    state['recent_samples']=list(samples)
    if durations:
        ordered=sorted(durations)
        state['callback_ms']={'p50':ordered[len(ordered)//2],'p95':ordered[int((len(ordered)-1)*.95)],'max':ordered[-1]}

def callback(*args):
    global stop_started
    began=time.perf_counter()
    with lock:
        if state['done']:
            return
        try:
            if threading.get_ident()!=threading.main_thread().ident:
                raise RuntimeError('CALLBACK_IS_NOT_GAME_PYTHON_MAIN_THREAD')
            state['callback_thread']=threading.get_ident()
            state['frames']+=1
            now=time.monotonic()
            scene_module=sys.modules.get('ui.UIArticleRole')
            if type(scene_module) is types.ModuleType:
                adapter.GK=vars(scene_module).get('GK')
            # Cheap function/code identity checks every frame; full normalized
            # hashes periodically and on every new binding. Each capability
            # latches independently, so display can survive a training update.
            compatibility.check('display')
            if options.get('battle_enabled'):
                compatibility.check('training')
            mode=pending['mode']
            if mode=='STOP' or stop_started is not None:
                if stop_started is None:
                    stop_started=now
                outcomes=controller.stop(now)
                state['restore_outcomes']={str(key):value for key,value in outcomes.items()}
                update_snapshot([],now,True)
                status=controller.status()
                if status['modified']==0 and status['pending_restores']==0 and status['unresolved']==0:
                    state.update(done=True,phase='STOPPED',finished_at=time.time())
                    controller.clear()
                    adapter._handles.clear()
                elif now-stop_started>=3:
                    state.update(done=True,phase='RESTORE_FAULT',finished_at=time.time())
                else:
                    state['phase']='RESTORING'
            else:
                if mode=='PAUSED' and controller.mode=='RUNNING':
                    controller.pause(now)
                elif mode=='RUNNING' and controller.mode=='PAUSED':
                    controller.resume(now)
                handles=adapter.discover()
                # Battle handles are kept from every battle world; --battle decides
                # whether they are used at all.
                handles=[handle for handle in handles if handle.kind=='display' or
                         bool(options.get('battle_enabled',False))]
                controller.tick(handles,now)
                update_snapshot(handles,now)
            state['command_sequence']=pending['sequence']
        except BaseException:
            state['error']=traceback.format_exc()
            if controller is not None:
                outcomes=controller.stop()
                state['restore_outcomes']={str(key):value for key,value in outcomes.items()}
                state['controller']=controller.status()
                state['modified']=state['controller']['modified']
            state.update(done=True,phase='FAULTED',finished_at=time.time())
        durations.append((time.perf_counter()-began)*1000)
        if not state['done']:
            try:
                scheduler(1,callback)
            except BaseException:
                state['schedule_error']=traceback.format_exc()
                if controller is not None:
                    controller.stop()
                    state['controller']=controller.status()
                    state['modified']=state['controller']['modified']
                state.update(done=True,phase='FAULTED',finished_at=time.time())
    if state['done'] and state.get('reporter_exited'):
        try:
            publish(retry=False,nonblocking=True)
        except BaseException:
            pass

def reporter():
    try:
        while True:
            command=None
            try:
                if os.path.getsize(CONTROL)>4096:
                    raise ValueError('CONTROL_TOO_LARGE')
                with open(CONTROL,'r',encoding='utf8') as handle:
                    command=json.load(handle)
            except (FileNotFoundError,json.JSONDecodeError):
                pass
            except Exception as error:
                with lock:
                    state['control_error']=str(error)[:240]
            with lock:
                if isinstance(command,dict) and command.get('run_id')==run_id:
                    mode=command.get('mode')
                    sequence=command.get('sequence')
                    if mode in ('RUNNING','PAUSED','STOP') and isinstance(sequence,str) and len(sequence)<=100:
                        pending.update(mode=mode,sequence=sequence)
                done=state['done']
            publish()
            if done:
                return
            time.sleep(.5)
    except BaseException:
        with lock:
            state['reporter_error']=traceback.format_exc()
            pending.update(mode='STOP',sequence='reporter-failure')
    finally:
        with lock:
            state['reporter_exited']=True
        try:
            publish()
        except BaseException:
            pass

try:
    if (type(expected_pid) is not int or expected_pid!=os.getpid() or
        not isinstance(run_id,str) or not run_id.startswith('persistent-') or
        len(run_id)!=43 or any(c not in '0123456789abcdef' for c in run_id[11:]) or
        type(options.get('battle_enabled')) is not bool):
        raise RuntimeError('INVALID_BOUND_PROCESS_OPTIONS')
    if existing is not None and unresolved(existing):
        raise RuntimeError('PERSISTENT_SESSION_ACTIVE_OR_RESTORE_UNRESOLVED')
    old_preview=sys.modules.get('friendswine_preview_5min')
    if old_preview is not None and (not old_preview.state.get('done') or
        (old_preview.state.get('modified') and old_preview.state.get('restore_result')!='TARGET_DESTROYED')):
        raise RuntimeError('FINITE_PREVIEW_STILL_ACTIVE')
    core=load_private('persistent_core.py','friendswine_core_private')
    training_core=load_private('training_core.py','friendswine_training_private')
    compat_code=load_private('runtime_compat.py','friendswine_compat_private')
    compatibility=compat_code.RuntimeCompatibility(options.get('compatibility_profile'))
    # Native function semantics are admitted by the host's native profile. The
    # runtime separately verifies structural identity before the first call.
    if not compatibility._native_ok():
        raise RuntimeError('UNSUPPORTED_NATIVE_RUNTIME_INTERFACE')
    scheduler=vars(sys.modules['game3d'])['frame_delay_exec']
    adapter_code=load_private('persistent_adapter.py','friendswine_adapter_private')
    scene_module=sys.modules.get('ui.UIArticleRole')
    GK=vars(scene_module).get('GK') if type(scene_module) is types.ModuleType else None
    adapter=adapter_code.NeoXAdapter(GK,sys.modules['math3d'],compatibility=compatibility)
    controller=CombinedController(core.Controller(adapter,settle_frames=6),
                                  training_core.TrainingController(adapter,settle_frames=6))
    session=types.ModuleType(REGISTRY)
    session.state=state
    session.pending=pending
    sys.modules[REGISTRY]=session
    publish()
    threading.Thread(target=reporter,name='friendswine-persistent-status',daemon=True).start()
    scheduler(1,callback)
except BaseException:
    with lock:
        state.update(error=traceback.format_exc(),done=True,phase='STARTUP_ERROR',finished_at=time.time())
    # Keep a rejected start separate from the active session's live status.
    if existing is not None and unresolved(existing):
        safe_id=run_id if isinstance(run_id,str) and all(c.isalnum() or c=='-' for c in run_id) else 'invalid'
        rejected=CHANNEL+'rejected-'+safe_id+'.json'
        with open(rejected,'w',encoding='utf8') as handle:
            json.dump(state,handle,ensure_ascii=False,indent=2)
    else:
        publish()
