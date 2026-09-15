"""One-time, idempotent migration seed for the Home Forecaster battery model."""
from datetime import timezone

SEED_ENTITY='sensor.home_energy_manager_battery_model_seed'


def seed_if_needed(client,store,model,now):
    if store.meta_get('battery_model_migration_state')=='seeded':return False
    state=client.state_optional(SEED_ENTITY)
    attrs=state.get('attributes',{}) if state else {}
    if attrs.get('schema_version')!=1 or attrs.get('source')!='controller_migration_seed':return False
    bands=attrs.get('bands')
    if not isinstance(bands,list) or len(bands)!=len(model.SOC_BANDS):return False
    by_range={}
    try:
        for band in bands:by_range[(float(band['soc_lo']),float(band['soc_hi']))]=band
        top=float(attrs['top_completion_allowance_minutes'])
    except (KeyError,TypeError,ValueError):return False
    if set(by_range)!=set((float(a),float(b)) for a,b in model.SOC_BANDS):return False
    stamp=now.astimezone(timezone.utc).isoformat()
    for lo,hi in model.SOC_BANDS:
        band=by_range[(float(lo),float(hi))]
        learned=band.get('learned_factor'); confidence=float(band.get('confidence') or 0)
        if learned is not None:learned=float(learned)
        store.db.execute(
            'INSERT INTO battery_charge_bands(band_lo,band_hi,learned_factor,confidence,full_equiv,obs_count,p10,p90,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(band_lo,band_hi) DO UPDATE SET learned_factor=excluded.learned_factor,confidence=excluded.confidence,full_equiv=excluded.full_equiv,obs_count=excluded.obs_count,p10=excluded.p10,p90=excluded.p90,updated_at=excluded.updated_at',
            (lo,hi,learned,confidence,float(band.get('full_equiv') or 0),int(band.get('samples') or 0),band.get('p10'),band.get('p90'),band.get('updated_at') or stamp))
    tc=attrs.get('top_completion') or {}
    store.meta_set('battery_model_top_completion_allowance_minutes',str(top))
    store.meta_set('battery_model_top_completion_attempts',str(int(tc.get('attempts') or 0)))
    store.meta_set('battery_model_top_completion_successes',str(int(tc.get('successes') or 0)))
    store.meta_set('battery_model_top_completion_misses',str(int(tc.get('misses') or 0)))
    store.meta_set('battery_model_seed_generated_at',str(attrs.get('generated_at') or ''))
    store.meta_set('battery_model_seeded_at',stamp)
    store.meta_set('battery_model_migration_state','seeded')
    store.db.commit()
    model.base.LOG.info('Battery model migration seeded from Controller: bands=%d top_completion=%.1fmin source_generated_at=%s',len(bands),top,attrs.get('generated_at'))
    return True
