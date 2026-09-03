/* ──────────────────────────────────────────────────────────────────────────
 * Evosep One — column health & clog early warning
 *
 * Paste this whole block into stan/dashboard/public/index.html immediately
 * ABOVE `function MaintenanceTab()`, then render <EvosepColumnPanel /> inside
 * MaintenanceTab's returned JSX.
 *
 * Data: GET /api/maintenance/evosep  (PG Farm first, config JSON fallback).
 * Uses STAN's own useFetch, .card, and theme CSS vars. Charts are inline SVG —
 * no new libraries; Plotly is left untouched.
 *
 * Colour: the three method hues are the dataviz dark categorical slots 1-3,
 * validated all-pairs against STAN's navy surface #022851 (worst CVD ΔE 9.4,
 * worst normal-vision ΔE 20.9, all ≥3:1 contrast). Severity uses STAN's
 * reserved status vars and ALWAYS carries a glyph + text label — warn↔pass
 * fails CVD separation (ΔE 4.2 protan), so colour alone would be unreadable.
 * ────────────────────────────────────────────────────────────────────────── */

/* Categorical slots 1-3 — assigned to methods in fixed order, never cycled. */
const EV_HUES = ['#3987e5', '#d95926', '#199e70'];

const EV_SEV = {
  critical: { color: 'var(--fail)', glyph: '▲', label: 'Critical' },
  elevated: { color: 'var(--warn)', glyph: '◆', label: 'Elevated' },
  ok:       { color: 'var(--pass)', glyph: '●', label: 'Normal'   },
};

function evFmtDate(s) {
  if (!s) return '—';
  return String(s).replace('T', ' ').slice(0, 16);
}
function evNum(v, d = 0) {
  return (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toFixed(d);
}

/* Severity chip: colour + glyph + word. Never colour alone. */
function EvBadge({ sev }) {
  const s = EV_SEV[sev] || EV_SEV.ok;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '0.3rem',
      color: s.color, fontSize: '0.78rem', fontWeight: 600, whiteSpace: 'nowrap',
    }}>
      <span aria-hidden="true">{s.glyph}</span>{s.label}
    </span>
  );
}

/* A headline number with its unit and a one-line explanation. */
function EvTile({ label, value, unit, note, tone }) {
  return (
    <div style={{
      background: 'var(--bg)', border: '1px solid var(--border)',
      borderRadius: '0.5rem', padding: '0.6rem 0.75rem', flex: '1 1 150px',
      minWidth: '140px',
    }}>
      <div style={{ color: 'var(--muted)', fontSize: '0.72rem', marginBottom: '0.2rem' }}>
        {label}
      </div>
      <div style={{ fontSize: '1.35rem', fontWeight: 700, color: tone || 'var(--text)', lineHeight: 1.1 }}>
        {value}
        {unit && <span style={{ fontSize: '0.8rem', fontWeight: 400, color: 'var(--muted)' }}> {unit}</span>}
      </div>
      {note && <div style={{ color: 'var(--muted)', fontSize: '0.7rem', marginTop: '0.2rem' }}>{note}</div>}
    </div>
  );
}

/* ── Chart A ───────────────────────────────────────────────────────────────
 * Column backpressure per run, one small multiple per analytical method.
 * Separate charts (never a shared/dual y-axis): a 100 spd and a 60 spd
 * gradient run at genuinely different pressures, so overlaying them on one
 * scale would compare nothing.
 * ────────────────────────────────────────────────────────────────────────── */
function EvBaselineChart({ method, ms, hue, flagsByRun }) {
  const series = ms.series || [];
  if (series.length < 2) return null;

  const W = 1000, H = 150, PADL = 46, PADR = 12, PADT = 12, PADB = 20;
  const times = series.map(p => new Date(p.start).getTime());
  const vals = series.map(p => p.plateau_bar);
  const t0 = Math.min(...times), t1 = Math.max(...times);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = Math.max(4, (hi - lo) * 0.12);
  const yLo = lo - pad, yHi = hi + pad;

  const X = t => PADL + ((t - t0) / ((t1 - t0) || 1)) * (W - PADL - PADR);
  const Y = v => PADT + (1 - (v - yLo) / ((yHi - yLo) || 1)) * (H - PADT - PADB);

  const path = series
    .map((p, i) => `${i ? 'L' : 'M'}${X(times[i]).toFixed(1)},${Y(p.plateau_bar).toFixed(1)}`)
    .join(' ');

  const base = ms.baseline_bar;
  const ticks = [yLo, (yLo + yHi) / 2, yHi];

  /* Sustained downward steps are candidate interventions — a new column, a
     wash, a seal service. They are what a rising baseline should be measured
     from, so they are drawn as rules rather than hidden. */
  const drops = (ms.steps || []).filter(s => s.direction === 'drop');

  return (
    <div style={{ marginBottom: '0.5rem' }}>
      <div style={{
        display: 'flex', alignItems: 'baseline', gap: '0.5rem',
        flexWrap: 'wrap', marginBottom: '0.15rem',
      }}>
        {/* Direct label — identity never rides on the line colour alone. */}
        <span style={{ color: hue, fontWeight: 700, fontSize: '0.85rem' }}>■ {method}</span>
        <span style={{ color: 'var(--muted)', fontSize: '0.75rem' }}>
          {ms.n_runs} runs · baseline {evNum(base, 0)} bar
          {ms.plateau_sd_bar != null && <> · run-to-run sd {evNum(ms.plateau_sd_bar, 1)} bar</>}
          {ms.trend_bar_per_day != null &&
            <> · trend {ms.trend_bar_per_day > 0 ? '+' : ''}{evNum(ms.trend_bar_per_day, 1)} bar/day</>}
        </span>
      </div>

      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }}
           role="img" aria-label={`${method} column backpressure per run`}>
        {ticks.map((v, i) => (
          <g key={i}>
            <line x1={PADL} x2={W - PADR} y1={Y(v)} y2={Y(v)}
                  stroke="var(--border)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
            <text x={PADL - 6} y={Y(v) + 3} textAnchor="end"
                  fill="var(--muted)" fontSize="10">{evNum(v, 0)}</text>
          </g>
        ))}

        {/* ±10 % band around the running baseline: the "normal" corridor. */}
        {base && (
          <rect x={PADL} y={Y(base * 1.1)} width={W - PADL - PADR}
                height={Math.max(1, Y(base * 0.9) - Y(base * 1.1))}
                fill={hue} opacity="0.10" />
        )}

        {drops.map((s, i) => {
          const x = X(new Date(s.at).getTime());
          return (
            <g key={i}>
              <line x1={x} x2={x} y1={PADT} y2={H - PADB}
                    stroke="var(--muted)" strokeWidth="1" strokeDasharray="3 3"
                    vectorEffect="non-scaling-stroke" opacity="0.7" />
              <title>{`Baseline step ${s.change_pct}% at ${evFmtDate(s.at)} — ${s.from_bar} → ${s.to_bar} bar (candidate column change / wash)`}</title>
            </g>
          );
        })}

        <path d={path} fill="none" stroke={hue} strokeWidth="2"
              vectorEffect="non-scaling-stroke" strokeLinejoin="round" />

        {/* Flagged runs carry a ring + status colour + hover text. */}
        {series.map((p, i) => {
          const f = flagsByRun[p.start];
          if (!f) return null;
          const s = EV_SEV[f.severity] || EV_SEV.elevated;
          return (
            <g key={i}>
              <circle cx={X(times[i])} cy={Y(p.plateau_bar)} r="4.5"
                      fill={s.color} stroke="var(--surface)" strokeWidth="2"
                      vectorEffect="non-scaling-stroke" />
              <title>{`${s.label} — ${evFmtDate(p.start)}\n${p.plateau_bar} bar\n${f.reasons.join('\n')}`}</title>
            </g>
          );
        })}

        {/* Invisible per-run hit targets so every point is hoverable. */}
        {series.map((p, i) => (
          <g key={`h${i}`}>
            <circle cx={X(times[i])} cy={Y(p.plateau_bar)} r="7" fill="transparent" />
            <title>{`${evFmtDate(p.start)} — ${p.plateau_bar} bar`}</title>
          </g>
        ))}

        <text x={PADL} y={H - 6} fill="var(--muted)" fontSize="10">
          {evFmtDate(ms.first_run)}
        </text>
        <text x={W - PADR} y={H - 6} textAnchor="end" fill="var(--muted)" fontSize="10">
          {evFmtDate(ms.last_run)}
        </text>
      </svg>
    </div>
  );
}

/* ── Chart B ───────────────────────────────────────────────────────────────
 * Anatomy of a clog. One small multiple per run that reached the pump
 * cut-out, each drawn against the ROLLING reference it was actually judged
 * against — the median of the last few clean runs of the same method,
 * interpolated onto absolute minutes.
 *
 * Absolute minutes, not relative: an aborting run is short *because* it
 * failed, so a relative-time axis compresses it and shifts every feature
 * earlier. The extractor aligns on the instrument's real schedule, and this
 * chart draws exactly what it compared.
 * ────────────────────────────────────────────────────────────────────────── */
function EvClogTrace({ run, ceiling }) {
  const g = run.ref_grid_min, ref = run.ref_curve, mine = run.curve_on_ref_grid;
  if (!g || !ref || !mine) return null;

  const W = 1000, H = 210, PADL = 44, PADR = 118, PADT = 14, PADB = 26;
  const tMax = Math.max(g[g.length - 1], run.duration_min);
  const yHi = ceiling * 1.04;
  const X = t => PADL + (t / tMax) * (W - PADL - PADR);
  const Y = v => PADT + (1 - v / yHi) * (H - PADT - PADB);

  const draw = arr => {
    let d = '', pen = false;
    arr.forEach((v, i) => {
      if (v === null || v === undefined) { pen = false; return; }
      d += `${pen ? 'L' : 'M'}${X(g[i]).toFixed(1)},${Y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };

  const bx = run.envelope_breach_min != null ? X(run.envelope_breach_min) : null;
  const yTicks = [0, 130, 260, 390, ceiling];

  return (
    <div style={{ marginBottom: '0.75rem' }}>
      <div style={{ fontSize: '0.8rem', marginBottom: '0.1rem' }}>
        <strong>{evFmtDate(run.start)}</strong>
        <span style={{ color: 'var(--muted)' }}>
          {' · '}{run.method}{' · '}peak {evNum(run.peak_bar, 0)} bar
          {run.envelope_lead_min != null && <>
            {' · '}<span style={{ color: 'var(--accent)' }}>
              {evNum(run.envelope_lead_min, 1)} min of warning
            </span> before the {run.envelope_lead_to || 'cut-out'}
          </>}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }}
           role="img" aria-label={`Pressure trace for the run at ${run.start}`}>
        {yTicks.map((v, i) => (
          <g key={i}>
            <line x1={PADL} x2={W - PADR} y1={Y(v)} y2={Y(v)}
                  stroke="var(--border)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
            <text x={PADL - 6} y={Y(v) + 3} textAnchor="end" fill="var(--muted)" fontSize="10">{v}</text>
          </g>
        ))}
        {[0, 0.25, 0.5, 0.75, 1].map((f, i) => (
          <text key={i} x={X(f * tMax)} y={H - 8} textAnchor="middle"
                fill="var(--muted)" fontSize="10">{(f * tMax).toFixed(0)} min</text>
        ))}

        {/* The window between leaving the reference and the abort — the
            warning this whole feature exists to produce. */}
        {bx !== null && (
          <rect x={bx} y={PADT}
                width={Math.max(0, X(run.t_ceiling_min != null ? run.t_ceiling_min : run.duration_min) - bx)}
                height={H - PADT - PADB} fill="var(--accent)" opacity="0.10" />
        )}

        <path d={draw(ref)} fill="none" stroke="var(--muted)" strokeWidth="2"
              vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
        <path d={draw(mine)} fill="none" stroke="var(--fail)" strokeWidth="2"
              vectorEffect="non-scaling-stroke" strokeLinejoin="round" />

        <line x1={PADL} x2={W - PADR} y1={Y(ceiling)} y2={Y(ceiling)}
              stroke="var(--fail)" strokeWidth="2" strokeDasharray="6 4"
              vectorEffect="non-scaling-stroke" />
        <text x={W - PADR + 6} y={Y(ceiling) + 4} fill="var(--fail)" fontSize="10" fontWeight="600">
          {ceiling} bar cut-out
        </text>

        {bx !== null && (
          <g>
            <line x1={bx} x2={bx} y1={PADT} y2={H - PADB} stroke="var(--accent)"
                  strokeWidth="2" strokeDasharray="4 3" vectorEffect="non-scaling-stroke" />
            <text x={bx + 5} y={PADT + 11} fill="var(--accent)" fontSize="10" fontWeight="600">
              departs at {evNum(run.envelope_breach_min, 1)} min
            </text>
          </g>
        )}

        {/* Legend — two series, so identity never rides on colour alone. */}
        <g transform={`translate(${W - PADR + 6},${PADT + 24})`}>
          <line x1="0" x2="14" y1="0" y2="0" stroke="var(--muted)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
          <text x="18" y="4" fill="var(--muted)" fontSize="10">expected</text>
          <line x1="0" x2="14" y1="15" y2="15" stroke="var(--fail)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
          <text x="18" y="19" fill="var(--fail)" fontSize="10">this run</text>
        </g>
      </svg>
    </div>
  );
}

/* ── Validation ────────────────────────────────────────────────────────────
 * Scores the pressure signal against Compass's own failure log. This is the
 * honesty panel: if the trace did not independently find the known failures,
 * it says so here.
 * ────────────────────────────────────────────────────────────────────────── */
function EvValidation({ v }) {
  if (!v || !v.available) return null;
  const rows = (v.matched || []).slice().sort((a, b) => (a.compass_time < b.compass_time ? 1 : -1));
  const clogOK = v.clog_failures_detected === v.clog_failures_in_window;
  const tipOK = v.tip_failures_detected === v.tip_failures_in_window;

  return (
    <div style={{ marginTop: '1rem' }}>
      <h4 style={{ fontSize: '0.9rem', color: 'var(--text)', margin: '0 0 0.35rem' }}>
        Does the pressure trace actually catch the failures Compass logged?
      </h4>
      <div style={{ display: 'flex', gap: '0.6rem', flexWrap: 'wrap', marginBottom: '0.6rem' }}>
        <EvTile label="LC clog failures found"
                value={`${v.clog_failures_detected}/${v.clog_failures_in_window}`}
                tone={clogOK ? 'var(--pass)' : 'var(--warn)'}
                note={clogOK ? 'every one, independently' : 'some were missed'} />
        <EvTile label="Evotip failures found"
                value={`${v.tip_failures_detected}/${v.tip_failures_in_window}`}
                tone={tipOK ? 'var(--pass)' : 'var(--warn)'}
                note={tipOK ? 'low-pressure side' : 'some were missed'} />
        <EvTile label="False negatives"
                value={(v.unmatched_compass_failures || []).length}
                note="Compass failures with no matching pressure record" />
        <EvTile label="Compass record ends"
                value={evFmtDate(v.compass_covers_to)}
                note="pressure logs run later than this" />
      </div>

      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', fontSize: '0.78rem' }}>
          <thead>
            <tr>
              <th>Compass logged</th><th>Category</th>
              <th style={{ textAlign: 'right' }}>Peak bar</th>
              <th style={{ textAlign: 'right' }}>Tip bar</th>
              <th style={{ textAlign: 'right' }}>Warning</th>
              <th>Found by pressure</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((m, i) => (
              <tr key={i}>
                <td style={{ whiteSpace: 'nowrap' }}>{evFmtDate(m.compass_time)}</td>
                <td style={{ color: 'var(--muted)' }}>{m.category}</td>
                <td style={{ textAlign: 'right', color: m.peak_bar >= 515 ? 'var(--fail)' : 'var(--text)' }}>
                  {evNum(m.peak_bar, 0)}
                </td>
                <td style={{ textAlign: 'right', color: m.tip_pressure_max_bar >= 60 ? 'var(--fail)' : 'var(--text)' }}>
                  {evNum(m.tip_pressure_max_bar, 0)}
                </td>
                <td style={{ textAlign: 'right' }}>
                  {m.envelope_lead_min != null
                    ? <span title="minutes between leaving the healthy envelope and hitting the pump cut-out">
                        {evNum(m.envelope_lead_min, 1)} min
                      </span>
                    : <span style={{ color: 'var(--muted)' }}>—</span>}
                </td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  {m.flagged_by_pressure
                    ? <span style={{ color: 'var(--pass)', fontWeight: 600 }}>
                        <span aria-hidden="true">✓ </span>Detected</span>
                    : <span style={{ color: 'var(--fail)', fontWeight: 600 }}>
                        <span aria-hidden="true">✕ </span>Missed</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(v.silent_critical_excursions || []).length > 0 && (
        <div style={{ color: 'var(--muted)', fontSize: '0.75rem', marginTop: '0.5rem' }}>
          {v.silent_critical_excursions.length} further run(s) inside the Compass-covered
          window ran at critical backpressure without Compass recording any failure —
          they completed, so the error log never mentions them.
        </div>
      )}
    </div>
  );
}

/* ── The panel ─────────────────────────────────────────────────────────── */
function EvosepColumnPanel() {
  const { data, loading, error } = useFetch('/api/maintenance/evosep', []);

  /* Match the Bruker panel: stay invisible until the extractor has produced
     a document, rather than showing an error box on a fresh install. */
  if (loading || error || !data || !data.summary) return null;

  const d = data;
  const col = d.column || {};
  const s = d.summary || {};
  const wear = d.wear || {};
  const flags = d.flags || [];
  const flagsByRun = {};
  flags.forEach(f => { flagsByRun[f.start] = f; });

  const analytical = Object.values(d.methods || {})
    .filter(m => m.analytical)
    .sort((a, b) => b.n_runs - a.n_runs);
  const primary = (d.column && d.column.method) || (analytical[0] && analytical[0].method);

  const rising = col.baseline_change_pct != null && col.baseline_change_pct > 0;
  const recent = flags.slice().sort((a, b) => (a.start < b.start ? 1 : -1)).slice(0, 12);
  /* Runs that reached the pump cut-out and carry the reference they were
     compared against — the ones worth drawing a full trace for. */
  const clogs = (d.runs || [])
    .filter(r => r.ref_curve && r.peak_bar >= (d.ceiling_bar - 5))
    .sort((a, b) => (a.start < b.start ? 1 : -1));

  return (
    <div className="card">
      <h3>Evosep One — column health &amp; clog early warning</h3>
      <div style={{ color: 'var(--muted)', fontSize: '0.8rem', marginBottom: '0.75rem' }}>
        {s.n_runs} procedure runs on {d.instrument_host} ({(d.serials || []).join(', ')}),
        {' '}{evFmtDate(s.first_run)} → {evFmtDate(s.last_run)}. Backpressure is read from
        {' '}<strong>{d.column_pump}</strong>, the analytical-column pump — it runs at
        {' '}300-520 bar and ~1.7 µL/min while the low-pressure pumps A-D stay under 10 bar.
        {' '}Extracted {evFmtDate(d.generated_at)}.
      </div>

      {/* ── Current column state ── */}
      <div style={{ display: 'flex', gap: '0.6rem', flexWrap: 'wrap', marginBottom: '0.9rem' }}>
        <EvTile label="Days on this column" value={evNum(col.days_since, 1)} unit="days"
                note={col.source === 'logged maintenance event'
                  ? 'since the logged column change'
                  : 'since an inferred baseline drop — no column change logged'} />
        <EvTile label="Injections on column" value={col.injections_since != null ? col.injections_since : '—'}
                note="from the instrument's own lifetime counter" />
        <EvTile label="Baseline now" value={evNum(col.baseline_now_bar, 0)} unit="bar"
                note={`was ${evNum(col.baseline_at_install_bar, 0)} bar when installed`} />
        <EvTile label="Baseline change" value={`${rising ? '+' : ''}${evNum(col.baseline_change_pct, 1)}%`}
                tone={col.baseline_change_pct > 15 ? 'var(--warn)' : 'var(--text)'}
                note={rising ? 'backpressure is climbing' : 'backpressure is flat or falling'} />
        <EvTile label="Runs at the cut-out" value={s.n_at_ceiling}
                tone={s.n_at_ceiling ? 'var(--fail)' : 'var(--pass)'}
                note={`peaked at the ${d.ceiling_bar} bar limit`} />
        <EvTile label="Flagged runs" value={`${s.n_flagged}`}
                tone={s.n_critical ? 'var(--warn)' : 'var(--text)'}
                note={`${s.n_critical} critical, of ${s.n_runs} runs`} />
      </div>

      {/* ── Chart A ── */}
      <h4 style={{ fontSize: '0.9rem', color: 'var(--text)', margin: '0 0 0.4rem' }}>
        Column backpressure, run by run
      </h4>
      <div style={{ color: 'var(--muted)', fontSize: '0.75rem', marginBottom: '0.5rem' }}>
        One panel per gradient, each on its own scale — a 100 spd and a 60 spd method
        run at different pressures, so a shared axis would compare nothing. The band is
        ±10 % of the running baseline; dashed rules are sustained baseline drops
        (a wash, a new column); rings are flagged runs.
      </div>
      {analytical.map((ms, i) => (
        <EvBaselineChart key={ms.method} method={ms.method} ms={ms}
                         hue={EV_HUES[i % EV_HUES.length]} flagsByRun={flagsByRun} />
      ))}

      {/* ── Chart B ── */}
      {clogs.length > 0 && (
        <div style={{ marginTop: '1rem' }}>
          <h4 style={{ fontSize: '0.9rem', color: 'var(--text)', margin: '0 0 0.4rem' }}>
            Anatomy of a clog — every run that reached the cut-out
          </h4>
          <div style={{ color: 'var(--muted)', fontSize: '0.75rem', marginBottom: '0.6rem' }}>
            Each run is drawn against the reference it was judged against: the median
            of the last few clean runs of the same method, on absolute minutes. A
            clogging run tracks that reference exactly, then departs and climbs to the
            cut-out. The shaded span is the warning — the time between the departure
            and the moment the run actually hit the limit.
          </div>
          {clogs.map(r => (
            <EvClogTrace key={r.run} run={r} ceiling={d.ceiling_bar} />
          ))}
        </div>
      )}

      {/* ── Validation ── */}
      <EvValidation v={d.validation} />

      {/* ── Triage list ── */}
      {recent.length > 0 && (
        <div style={{ marginTop: '1rem' }}>
          <h4 style={{ fontSize: '0.9rem', color: 'var(--text)', margin: '0 0 0.35rem' }}>
            Most recent flagged runs
          </h4>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', fontSize: '0.78rem' }}>
              <thead>
                <tr>
                  <th>When</th><th>Method</th>
                  <th style={{ textAlign: 'right' }}>Plateau</th>
                  <th style={{ textAlign: 'right' }}>vs baseline</th>
                  <th>State</th><th>Why</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((f, i) => (
                  <tr key={i}>
                    <td style={{ whiteSpace: 'nowrap' }}>{evFmtDate(f.start)}</td>
                    <td style={{ color: 'var(--muted)' }}>{f.method}</td>
                    <td style={{ textAlign: 'right' }}>{evNum(f.plateau_bar, 0)} bar</td>
                    <td style={{ textAlign: 'right' }}>
                      {f.pct_over_baseline != null
                        ? `${f.pct_over_baseline > 0 ? '+' : ''}${evNum(f.pct_over_baseline, 0)}%`
                        : '—'}
                    </td>
                    <td><EvBadge sev={f.severity} /></td>
                    <td style={{ color: 'var(--muted)' }}>{f.reasons.join(' · ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Wear counters ── */}
      {wear.pump_seal_ml && (
        <div style={{ marginTop: '1rem' }}>
          <h4 style={{ fontSize: '0.9rem', color: 'var(--text)', margin: '0 0 0.35rem' }}>
            Instrument wear — from the Evosep's own lifetime counters
          </h4>
          <div style={{ display: 'flex', gap: '0.6rem', flexWrap: 'wrap' }}>
            <EvTile label="Total analyses" value={(wear.total_analyses || 0).toLocaleString()}
                    note={`${evNum(wear.analyses_per_day, 1)}/day over the last ${evNum(wear.window_days, 0)} days`} />
            {Object.entries(wear.pump_seal_ml).map(([p, v]) => (
              <EvTile key={p} label={`${p} seal`} value={(v.ml || 0).toLocaleString()} unit="mL"
                      note={`+${evNum(v.ml_per_day, 1)} mL/day`} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
