"""Actual million-sweep history on the supplied packaged core, without USB."""
from dataclasses import replace
from pathlib import Path
import shutil
import uuid

from ..config import Config,BenchmarkError
from ..macos_rc import host
from ..runner import output_directory,atomic_report,run
from ..source import encode,sweep
from .evidence import bundle_identity,verify_host
from .node import PackageNode
from .resources import owned_measurement_pids,assess_resources


def history_config(output,*,allow_million=False):
    if allow_million is not True:
        raise BenchmarkError('explicit_opt_in_required','The retained million-sweep archive requires --allow-million')
    return Config('http://127.0.0.1:1',Path(output),mode='history',workload_profile='macos-rc1',
        run_id='machistory-'+uuid.uuid4().hex,sweeps=1_000_000,points=290,batch_size=100,allow_million=True)


def run_history_check(app,manifest,output,*,allow_million=False):
    root=output_directory(output).resolve(strict=True)
    report=dict(profile='macos-million-history',successful=False,point2_passed=False,point4_passed=False,
        hardware_tested=False,errors=[],host=host())
    node=None
    try:
        config=history_config(root/'producer',allow_million=allow_million)
        if not verify_host(report['host']):raise BenchmarkError('wrong_host','Run on the target Apple Silicon Mac/macOS27.3 with16+GiB')
        identity=bundle_identity(app,manifest);report['identity']=identity
        estimated=max(len(encode(sweep(config,i))) for i in (0,500_000,999_999))*config.sweeps*3+256*1024**2
        free=shutil.disk_usage(root).free
        report['archive_budget']=dict(estimated_peak_bytes=estimated,free_bytes=free,automatic_deletion=False)
        if free<estimated:raise BenchmarkError('insufficient_space','Million-sweep archive, ledger and diagnostics do not fit')
        out=root/'node';out.mkdir(mode=0o700)
        # History proves every offered/archive source through the bounded ledger.
        # It does not use frame/commit timing or private backup triggers. Keeping
        # per-commit diagnostics would overflow their 256MiB load/hardware quota.
        node=PackageNode(Path(app)/'Contents/Helpers/ori-core',out,config.run_id,commit_diagnostics=False)
        report['commit_diagnostics']=dict(status='not_enabled',scope='History-only; full offered/archive proof and RSS retained; no commit or presentation timing claim')
        config=replace(config,base_url=node.start())
        config=replace(config,pids=owned_measurement_pids(node.binary,node.owner,None))
        produced,status=run(config)
        report['production']=produced
        report['resources']=assess_resources(produced.get('resources',{}),
            minimum_seconds=produced.get('production',{}).get('preload_elapsed_seconds',1),required=('core','background'))
        target=produced.get('checks',{}).get('history_summary_p95',{})
        report['successful']=status==0 and target.get('status')=='passed' \
            and target.get('reference_workload_matched') is True and report['resources']['status']=='passed'
        node.close()
        if bundle_identity(app,manifest)!=identity:raise BenchmarkError('candidate_changed','App changed during history check')
        report['candidate_verified_after_run']=True
    except (Exception,KeyboardInterrupt) as error:
        report['errors'].append(dict(code=getattr(error,'code','history_failed'),message=str(error)))
    finally:
        if node:
            try:node.close(expect_success=False)
            except Exception as error:report['errors'].append(dict(code='cleanup_failed',message=str(error)))
            report['owned_nodes']=node.records
        report['successful']=report['successful'] and not report['errors']
        atomic_report(root,report)
    return report,0 if report['successful'] else 1
