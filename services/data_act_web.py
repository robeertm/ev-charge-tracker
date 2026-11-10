# -*- coding: utf-8 -*-
"""Web side of the XPENG Data Act import: upload page, background job,
preview, apply and undo.

The export is large (a month is ~340 MB unpacked), so a file is saved to disk
as it arrives, analysed in a background thread with a progress figure, and
deleted again once its events have been derived. Only the derived events (a
few KB) are kept until the user presses *Import*.
"""
import json
import logging
import os
import shutil
import threading
import time
import uuid

from flask import flash, jsonify, render_template, request

logger = logging.getLogger(__name__)

_JOBS = {}                       # job id -> state (in memory; results on disk)
_LOCK = threading.Lock()
_MAX_JOBS_KEPT = 20
_JOB_ID_OK = set('0123456789abcdef')


def _dirs(data_dir):
    base = os.path.join(data_dir, 'data_act')
    return {k: os.path.join(base, k) for k in ('uploads', 'jobs')}


def _safe_id(job_id):
    return bool(job_id) and len(job_id) == 12 and set(job_id) <= _JOB_ID_OK


def _set(job_id, **kw):
    with _LOCK:
        _JOBS.setdefault(job_id, {}).update(kw)


def _get(job_id):
    with _LOCK:
        return dict(_JOBS.get(job_id) or {})


def _purge_old(data_dir):
    """Keep the disk tidy: old job files and any upload folder that outlived
    its job (a restart in the middle of an analysis)."""
    d = _dirs(data_dir)
    cutoff = time.time() - 3 * 86400
    for sub in ('jobs', 'uploads'):
        try:
            for name in os.listdir(d[sub]):
                p = os.path.join(d[sub], name)
                if os.path.getmtime(p) < (cutoff if sub == 'jobs' else time.time() - 3600):
                    shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) \
                        else os.remove(p)
        except OSError:
            pass


def register(app, *, data_dir, write_guard, active_vehicle_id, battery_kwh_fn,
             efficiency_fn, release_fn, t, backup_fn=None, co2_fn=None,
             distance_label=None):
    from services import data_act as da
    from services import data_act_apply as ap

    def vehicles():
        from models.database import Vehicle
        return Vehicle.query.order_by(Vehicle.is_archived.asc(),
                                      Vehicle.id.asc()).all()

    @app.route('/data-act')
    def data_act_page():
        picker = active_vehicle_id()
        vs = vehicles()
        default = picker if isinstance(picker, int) else (
            next((v.id for v in vs if v.api_brand == 'homeassistant'),
                 vs[0].id if vs else None))
        last = None
        try:
            j = ap.last_journal(data_dir)
            if j:
                last = {'when': j['at'][:16],
                        'new': len(j['charges_new']) + len(j['trips_new']),
                        'changed': len(j['charges_changed']) + len(j['trips_changed'])}
        except Exception:
            logger.warning('data act journal unreadable', exc_info=True)
        return render_template('data_act.html', vehicles=vs,
                               default_vehicle=default, last_import=last)

    def _err(code, status=400, **params):
        return jsonify({'error': t('dataact.err_' + code, **params),
                        'code': code}), status

    @app.route('/api/data-act/upload', methods=['POST'])
    def data_act_upload():
        bad = write_guard()
        if bad:
            return bad
        files = [f for f in request.files.getlist('export_files') if f and f.filename]
        if not files:
            return _err('no_file')
        if request.content_length and request.content_length > da.MAX_UPLOAD_BYTES:
            return _err('too_large', 413, gb=da.MAX_UPLOAD_BYTES // 1024 ** 3)
        try:
            vid = int(request.form.get('vehicle_id') or 0)
        except ValueError:
            vid = 0
        from models.database import Vehicle, db
        veh = db.session.get(Vehicle, vid) if vid else None
        if veh is None:
            return _err('no_vehicle')
        job_id = uuid.uuid4().hex[:12]
        d = _dirs(data_dir)
        folder = os.path.join(d['uploads'], job_id)
        os.makedirs(folder, exist_ok=True)
        os.makedirs(d['jobs'], exist_ok=True)
        saved = []
        try:
            for n, f in enumerate(files):
                name = os.path.basename(f.filename) or 'file%d' % n
                path = os.path.join(folder, '%03d_%s' % (n, name.replace('/', '_')))
                f.save(path)
                saved.append((path, name))
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            logger.exception('data act upload could not be stored')
            return _err('store_failed', 500)
        _purge_old(data_dir)
        _set(job_id, state='running', stage='read', progress=0.0, rows=0,
             vehicle_id=vid, force=request.form.get('force') == '1',
             started=time.time())
        threading.Thread(target=_run, args=(job_id, folder, saved, vid),
                         daemon=True, name='data-act-%s' % job_id).start()
        return jsonify({'ok': True, 'job': job_id})

    def _run(job_id, folder, saved, vid):
        sources = []
        try:
            for path, name in saved:
                sources += da.sources_from_path(path, name)

            def cb(stage, frac, rows):
                _set(job_id, stage=stage, progress=round(frac, 3), rows=rows)
            try:
                result = da.analyze(sources, cb)
            except da.DataActError as e:
                probe = da.save_probe(sources, data_dir, e.code)
                raise _Failed(e.code, e.params, probe)
            except Exception:
                logger.exception('data act analysis failed')
                probe = da.save_probe(sources, data_dir, 'exception')
                raise _Failed('unreadable', {}, probe)
            if not result['charges'] and not result['trips']:
                # Readable, but nothing derived: keep a probe, it may be a
                # layout we did not expect (units, other signal names).
                da.save_probe(sources, data_dir, 'nothing_derived',
                              note=json.dumps({'rows': result['rows'],
                                               'streams': result['streams']}))
            with app.app_context():
                from models.database import Vehicle, db
                veh = db.session.get(Vehicle, vid)
                mismatch = ap.vin_mismatch(veh, result['vin'])
                if mismatch and not _get(job_id).get('force'):
                    raise _Failed('vin_mismatch', {}, None)
                plan = ap.make_plan(result, vid, battery_kwh_fn(vid))
            keep = dict(result)
            keep['plan'] = plan
            keep['vin'] = ''          # never stored; only a short tail for display
            with open(os.path.join(_dirs(data_dir)['jobs'], job_id + '.json'),
                      'w', encoding='utf-8') as fh:
                json.dump({'result': keep, 'vehicle_id': vid,
                           'vin_tail': (result['vin'] or '')[-4:]}, fh)
            _set(job_id, state='ready', stage='done', progress=1.0,
                 summary=_summary(result, plan),
                 vin_tail=(result['vin'] or '')[-4:])
        except _Failed as f:
            _set(job_id, state='error', code=f.code, params=f.params,
                 probe=bool(f.probe))
        except Exception:
            logger.exception('data act job failed')
            _set(job_id, state='error', code='unreadable', params={}, probe=False)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    class _Failed(Exception):
        def __init__(self, code, params, probe):
            super().__init__(code)
            self.code, self.params, self.probe = code, params, probe

    def _rows_for_ui(plan, limit=60):
        def pick(items):
            return [i for i in items if i['status'] in ('new', 'merge', 'extend')][:limit]
        return {'charges': pick(plan['charges']), 'trips': pick(plan['trips'])}

    def _summary(result, plan):
        ws, we = result['window']
        return {
            'window': [ws, we], 'rows': result['rows'],
            'model': result['model'], 'streams': result['streams'],
            'exports': result['exports'],
            'counts': plan['summary'],
            'has_charge_signal': result['has_charge_signal'],
            'has_trip_signal': result['has_trip_signal'],
            'km': round(sum(x['distance_km'] for x in plan['trips']
                            if x['status'] in ('new', 'extend')), 1),
            'kwh': round(sum((x.get('kwh') or 0) for x in plan['charges']
                             if x['status'] in ('new', 'extend')), 1),
            'rows_ui': _rows_for_ui(plan),
        }

    @app.route('/api/data-act/job/<job_id>')
    def data_act_job(job_id):
        if not _safe_id(job_id):
            return _err('unknown_job', 404)
        s = _get(job_id)
        if not s:
            return _err('unknown_job', 404)
        out = {'state': s.get('state'), 'stage': s.get('stage'),
               'progress': s.get('progress', 0), 'rows': s.get('rows', 0)}
        if s.get('state') == 'error':
            out['error'] = t('dataact.err_' + s['code'], **(s.get('params') or {}))
            out['code'] = s['code']
            out['probe'] = bool(s.get('probe'))
        if s.get('state') in ('ready', 'applied'):
            out['summary'] = s.get('summary')
            out['vin_tail'] = s.get('vin_tail')
        if s.get('state') == 'applied':
            out['applied'] = s.get('applied')
        return jsonify(out)

    @app.route('/api/data-act/apply/<job_id>', methods=['POST'])
    def data_act_apply(job_id):
        bad = write_guard()
        if bad:
            return bad
        if not _safe_id(job_id):
            return _err('unknown_job', 404)
        p = os.path.join(_dirs(data_dir)['jobs'], job_id + '.json')
        if not os.path.exists(p) or _get(job_id).get('state') == 'applied':
            return _err('unknown_job', 404)
        with open(p, encoding='utf-8') as fh:
            saved = json.load(fh)
        vid = saved['vehicle_id']
        result = saved['result']
        result['vin'] = ''
        try:
            if backup_fn:
                backup_fn()
        except Exception:
            logger.warning('pre-import database copy failed', exc_info=True)
        try:
            # Planned again against the database as it is now: another import
            # or a live charge may have landed since the preview.
            plan = ap.make_plan(result, vid, battery_kwh_fn(vid))
            n = ap.apply_plan(plan, vid, data_dir, job_id,
                              battery_kwh_fn(vid), efficiency_fn(vid))
        except Exception:
            logger.exception('data act apply failed')
            from models.database import db
            db.session.rollback()
            return _err('apply_failed', 500)
        if n['charges_new'] and co2_fn:
            try:
                co2_fn()
            except Exception:
                logger.warning('CO2 backfill after import not started', exc_info=True)
        _set(job_id, state='applied', applied=n)
        msg = t('dataact.applied', charges=n['charges_new'] + n['charges_merged']
                + n['charges_extended'], trips=n['trips_new'] + n['trips_extended'])
        logger.info('data act import applied: %s', n)
        flash(msg, 'success')
        try:
            os.remove(p)
        except OSError:
            pass
        return jsonify({'ok': True, 'message': msg, **n})

    @app.route('/api/data-act/undo', methods=['POST'])
    def data_act_undo():
        bad = write_guard()
        if bad:
            return bad
        try:
            r = ap.undo_last(data_dir, release_fn=release_fn)
        except Exception:
            logger.exception('data act undo failed')
            from models.database import db
            db.session.rollback()
            return _err('apply_failed', 500)
        if r is None:
            return _err('undo_none', 404)
        return jsonify({'ok': True,
                        'message': t('dataact.undone', removed=r['removed'],
                                     restored=r['restored']), **r})
