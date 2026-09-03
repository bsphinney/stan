/* ===========================================================================
 * STAN integration snippet  --  Bruker acquisition-health panel (React)
 *
 * Matches STAN's existing frontend patterns: React 18 UMD + in-browser Babel,
 * the `useFetch(url, deps)` helper, `className="card"`, and the theme CSS
 * variables (--surface, --border, --accent, --muted, --text, --pass/--warn/--fail).
 *
 * The three data hues (blue/orange/aqua) are the dataviz-skill-validated dark
 * categorical slots 1-3; they pass CVD + contrast on STAN's navy surface.
 * Plate status uses STAN's own --pass/--fail/--warn AND a glyph + legend, so
 * status never rides on colour alone (red/green are indistinguishable to
 * ~8% of viewers).
 *
 * WHERE TO PASTE
 *   1. Drop this whole block in index.html just ABOVE `function MaintenanceTab()`
 *      (search for that string; it's ~line 4575).
 *   2. Inside MaintenanceTab's returned JSX, add <BrukerAcquisitionPanel /> as
 *      the last child, right before the final closing </div> of the return.
 *      (One line -- see snippet_maintenance_wiring.jsx.)
 * ======================================================================== */

const BRK = {
  s1: '#3987e5',   // blue   -- throughput completed, method usage
  s2: '#d95926',   // orange -- failure taxonomy
  s3: '#199e70',   // aqua   -- duty cycle
  pass: 'var(--pass)', fail: 'var(--fail)', warn: 'var(--warn)',
  grid: 'var(--border)', ink: 'var(--text)', mut: 'var(--muted)', faint: '#7f97b5',
};
const bFmt = n => (n == null ? '–' : Number(n).toLocaleString('en-US'));
const B_ROWS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
const bCatClass = c => ({
  'Evotip missing / not picked up': { bg: 'rgba(217,89,38,.18)', fg: '#f3b79b' },
  'LC pressure / clog': { bg: 'rgba(250,178,25,.16)', fg: '#f6d68a' },
  'MS / acquisition software error': { bg: 'rgba(57,135,229,.16)', fg: '#a9ccf6' },
  'Connection lost': { bg: 'rgba(147,133,233,.18)', fg: '#c9c0f4' },
}[c] || { bg: 'rgba(160,180,204,.15)', fg: 'var(--muted)' });

function BrukerAcquisitionPanel() {
  const { data, loading, error } = useFetch('/api/maintenance/bruker', []);
  if (loading) return <div className="card"><h3>Bruker instrument health</h3>
    <p style={{ color: 'var(--muted)' }}>Loading acquisition history…</p></div>;
  // 404 = extractor hasn't run yet: hide the panel rather than shout.
  if (error) return null;
  if (!data || !data.summary) return null;
  const s = data.summary, idle = data.idle || {};
  const utilPct = s.span_days ? Math.round(100 * s.active_days / s.span_days) : 0;
  const tiles = [
    ['Acquisitions', bFmt(s.total_runs), `${bFmt(s.span_days)} days on record`],
    ['Success rate', `${s.success_rate_pct}%`, `${bFmt((s.failed || 0) + (s.aborted || 0))} failed / aborted`],
    ['Failed runs', bFmt((s.failed || 0) + (s.aborted || 0)), `${(100 - s.success_rate_pct).toFixed(1)}% of all runs`],
    ['Median runtime', `${s.median_run_min} min`, 'per completed acquisition'],
    ['Days in use', bFmt(s.active_days), `${utilPct}% of calendar days`],
    ['Longest idle gap', `${idle.longest_gap_days} d`, `${idle.longest_gap_start} → ${idle.longest_gap_end}`],
  ];
  const inst = data.instrument || {};
  return (
    <div>
      <div className="card">
        <h3>Bruker instrument health</h3>
        <p style={{ color: 'var(--muted)', fontSize: '0.85rem', marginBottom: '0.8rem' }}>
          Acquisition history for station <b style={{ color: 'var(--text)' }}>{inst.name}</b> from
          the Bruker Compass Server database ({s.first_run} → {String(s.last_run).slice(0, 10)}).
          Snapshot {data.backup_date}. Read-only — the dashboard never touches the instrument DB.
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: '0.7rem' }}>
          {tiles.map(([l, v, n]) => (
            <div key={l} style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: '0.6rem', padding: '0.7rem 0.8rem' }}>
              <div style={{ color: 'var(--muted)', fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '.4px' }}>{l}</div>
              <div style={{ fontSize: '1.5rem', fontWeight: 650, marginTop: '.1rem' }}>{v}</div>
              <div style={{ color: '#7f97b5', fontSize: '0.72rem', marginTop: '.15rem' }}>{n}</div>
            </div>
          ))}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(340px,1fr))', gap: '1rem' }}>
        <BThroughput months={data.throughput_monthly || []} />
        <BDuty months={data.utilization_monthly || []} />
      </div>

      <BPlate plate={data.latest_plate || {}} />

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(340px,1fr))', gap: '1rem' }}>
        <BFailBars cats={data.failures_by_category || []} first={s.first_run} />
        <BMethodBars methods={data.methods || []} />
      </div>

      <BFailTable rows={(data.failures_recent || []).slice(0, 12)} />
    </div>
  );
}

function BLegend({ items }) {
  return <div style={{ display: 'flex', flexWrap: 'wrap', gap: '.9rem', fontSize: '.8rem', color: 'var(--muted)', margin: '.1rem 0 .5rem' }}>
    {items.map(([c, l]) => <span key={l} style={{ display: 'inline-flex', alignItems: 'center', gap: '.35rem' }}>
      <span style={{ width: '.75rem', height: '.75rem', borderRadius: 2, background: c, display: 'inline-block' }} />{l}</span>)}
  </div>;
}

/* ---- monthly throughput: stacked columns, completed (blue) + failed (red) ---- */
function BThroughput({ months }) {
  const data = months.slice(-24);
  const W = 560, H = 220, L = 44, R = 12, T = 10, B = 34, iw = W - L - R, ih = H - T - B;
  const max = Math.max(1, ...data.map(d => d.total)) * 1.08;
  const step = iw / Math.max(data.length, 1), bw = Math.min(22, step * 0.66);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => max * f);
  return (
    <div className="card">
      <h3>Monthly acquisition throughput</h3>
      <p style={{ color: 'var(--muted)', fontSize: '.82rem', margin: '0 0 .6rem' }}>Completed and failed runs per month (last {data.length}). Hover for counts.</p>
      <BLegend items={[[BRK.s1, 'Completed'], [BRK.fail, 'Failed / aborted']]} />
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', overflow: 'visible' }}>
        {ticks.map((v, i) => { const y = T + ih - ih * (v / max); return <g key={i}>
          <line x1={L} y1={y} x2={L + iw} y2={y} stroke="var(--border)" strokeWidth="1" />
          <text x={L - 8} y={y + 3.5} textAnchor="end" fill="#7f97b5" fontSize="11">{Math.round(v).toLocaleString()}</text>
        </g>; })}
        {data.map((d, i) => {
          const cx = L + step * i + step / 2, x = cx - bw / 2;
          const hTot = ih * (d.total / max), hFail = ih * (d.failed / max), yTot = T + ih - hTot;
          return <g key={i}>
            {hTot - hFail > 0 && <rect x={x} y={yTot + hFail} width={bw} height={hTot - hFail} rx="2" fill={BRK.s1} />}
            {hFail > 0 && <rect x={x} y={yTot} width={bw} height={Math.max(hFail - 1, 1.5)} rx="2" fill={BRK.fail} />}
            <rect x={x - step * 0.17} y={T} width={step * 0.9} height={ih} fill="transparent">
              <title>{`${d.month}\nTotal ${bFmt(d.total)} · completed ${bFmt(d.done)} · failed ${bFmt(d.failed)}`}</title>
            </rect>
            {(i % 3 === 0 || i === data.length - 1) && <text x={cx} y={H - B + 16} textAnchor="middle" fill="#7f97b5" fontSize="11">{d.month.slice(2)}</text>}
          </g>;
        })}
      </svg>
    </div>
  );
}

/* ---- duty cycle line (aqua) ---- */
function BDuty({ months }) {
  const data = months.slice(-24);
  if (!data.length) return null;
  const W = 560, H = 200, L = 40, R = 14, T = 10, B = 32, iw = W - L - R, ih = H - T - B;
  const max = Math.max(10, ...data.map(d => d.duty_pct)) * 1.12;
  const step = iw / Math.max(data.length - 1, 1);
  const pts = data.map((d, i) => [L + step * i, T + ih - ih * (d.duty_pct / max)]);
  const area = `M${pts[0][0]},${T + ih} ` + pts.map(p => `L${p[0]},${p[1]}`).join(' ') + ` L${pts[pts.length - 1][0]},${T + ih} Z`;
  const last = pts[pts.length - 1];
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => max * f);
  return (
    <div className="card">
      <h3>Instrument duty cycle</h3>
      <p style={{ color: 'var(--muted)', fontSize: '.82rem', margin: '0 0 .6rem' }}>Share of each month's hours spent acquiring (run time ÷ hours in month).</p>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', overflow: 'visible' }}>
        {ticks.map((v, i) => { const y = T + ih - ih * (v / max); return <g key={i}>
          <line x1={L} y1={y} x2={L + iw} y2={y} stroke="var(--border)" strokeWidth="1" />
          <text x={L - 8} y={y + 3.5} textAnchor="end" fill="#7f97b5" fontSize="11">{Math.round(v)}%</text>
        </g>; })}
        <path d={area} fill={BRK.s3} fillOpacity="0.12" />
        <polyline points={pts.map(p => p.join(',')).join(' ')} fill="none" stroke={BRK.s3} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
        <circle cx={last[0]} cy={last[1]} r="4" fill={BRK.s3} stroke="var(--surface)" strokeWidth="2" />
        <text x={last[0] - 4} y={last[1] - 9} textAnchor="end" fill="var(--text)" fontSize="11" fontWeight="600">{data[data.length - 1].duty_pct}%</text>
        {data.map((d, i) => <g key={i}>
          <rect x={pts[i][0] - step / 2} y={T} width={step} height={ih} fill="transparent">
            <title>{`${d.month}\nDuty ${d.duty_pct}% · ${d.active_days} active days · ${bFmt(d.runs)} runs`}</title>
          </rect>
          {(i % 3 === 0 || i === data.length - 1) && <text x={pts[i][0]} y={H - B + 16} textAnchor="middle" fill="#7f97b5" fontSize="11">{d.month.slice(2)}</text>}
        </g>)}
      </svg>
    </div>
  );
}

/* ---- latest 96-well plate, status by glyph + colour ---- */
function BPlate({ plate }) {
  const wells = plate.wells || [];
  const byWell = {};
  wells.forEach(w => { const m = (w.well || '').match(/-([A-H])(\d{1,2})$/); if (m) byWell[m[1] + (+m[2])] = w; });
  const fails = wells.filter(w => w.status === 'FAILED' || w.status === 'ABORTED');
  const cell = (cls, glyph, key, tip) => {
    const bg = { pass: 'var(--pass)', fail: 'var(--fail)', run: 'var(--warn)', empty: 'var(--bg)' }[cls];
    return <div key={key} title={tip} style={{
      aspectRatio: '1 / 1', borderRadius: '.32rem', display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontSize: '.7rem', fontWeight: 700, color: cls === 'fail' ? '#fff' : '#04223f', background: bg,
      border: cls === 'empty' ? '1px solid var(--border)' : '1px solid transparent',
      boxShadow: cls === 'fail' ? '0 0 0 2px rgba(239,68,68,.35)' : 'none',
    }}>{glyph}</div>;
  };
  return (
    <div className="card">
      <h3>Latest plate — {plate.date}</h3>
      <p style={{ color: 'var(--muted)', fontSize: '.82rem', margin: '0 0 .6rem' }}>
        Plate S5, 96-well layout from the acquisition queue: {plate.n_pass} completed, {plate.n_fail} failed.
      </p>
      <BLegend items={[['var(--pass)', 'Completed ✓'], ['var(--fail)', 'Failed ✗'], ['var(--warn)', 'Running ◐'], ['var(--bg)', 'No run']]} />
      <div style={{ display: 'grid', gridTemplateColumns: '1.4rem repeat(12,1fr)', gap: 4, maxWidth: 640 }}>
        <div />
        {Array.from({ length: 12 }, (_, i) => <div key={'h' + i} style={{ textAlign: 'center', color: '#7f97b5', fontSize: '.72rem' }}>{i + 1}</div>)}
        {B_ROWS.map(r => [
          <div key={'r' + r} style={{ display: 'flex', alignItems: 'center', color: '#7f97b5', fontSize: '.72rem' }}>{r}</div>,
          ...Array.from({ length: 12 }, (_, c) => {
            const w = byWell[r + (c + 1)];
            if (!w) return cell('empty', '', r + '-' + c, `${r}${c + 1} — no run`);
            const cls = w.status === 'DONE' ? 'pass' : w.status === 'RUNNING' ? 'run' : 'fail';
            const glyph = cls === 'pass' ? '✓' : cls === 'run' ? '◐' : '✗';
            const tip = `${w.well} · ${w.sample || '—'}\n${w.status}${w.status !== 'DONE' ? '\n' + w.message : ''}`;
            return cell(cls, glyph, r + '-' + c, tip);
          }),
        ])}
      </div>
      {fails.length > 0 && (
        <div style={{ background: 'rgba(239,68,68,.08)', borderLeft: '3px solid var(--fail)', padding: '.6rem .8rem', borderRadius: '.35rem', fontSize: '.86rem', marginTop: '.8rem', color: '#f3c2c2' }}>
          {fails.length} well{fails.length > 1 ? 's' : ''} produced no usable data file:{' '}
          {fails.map((w, i) => <span key={i}><b style={{ color: 'var(--text)' }}>{w.well}</b> ({w.sample}){i < fails.length - 1 ? ', ' : ''}</span>)}.
          {' '}Reason parsed from the instrument: <b>{fails[0].category}</b>. Reload the tip(s) and re-queue just these wells.
        </div>
      )}
    </div>
  );
}

/* ---- failure taxonomy (orange bars) ---- */
function BFailBars({ cats, first }) {
  const data = [...cats].sort((a, b) => b.count - a.count);
  const total = data.reduce((a, d) => a + d.count, 0) || 1;
  const W = 560, rowH = 34, H = 12 + data.length * rowH, barX = 210, barMax = W - barX - 46;
  const max = Math.max(1, ...data.map(d => d.count));
  const top = data[0];
  return (
    <div className="card">
      <h3>Why acquisitions fail</h3>
      <p style={{ color: 'var(--muted)', fontSize: '.82rem', margin: '0 0 .6rem' }}>Every failed / aborted run since {first}, grouped by root cause from the instrument error text.</p>
      {top && <div style={{ background: 'rgba(217,89,38,.08)', borderLeft: '3px solid ' + BRK.s2, padding: '.6rem .8rem', borderRadius: '.35rem', fontSize: '.86rem', margin: '0 0 .8rem', color: '#f2d9cc' }}>
        <b style={{ color: 'var(--text)' }}>{top.category}</b> is the dominant failure mode — {bFmt(top.count)} runs ({Math.round(100 * top.count / total)}%). A consumable / loading issue on the Evosep, not an MS fault.
      </div>}
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', overflow: 'visible' }}>
        {data.map((d, i) => {
          const y = 6 + i * rowH + rowH / 2, bw = Math.max(barMax * (d.count / max), 2);
          return <g key={i}>
            <text x={0} y={y + 4} fill="var(--muted)" fontSize="11">{d.category}</text>
            <rect x={barX} y={y - 9} width={bw} height={18} rx="3" fill={BRK.s2} />
            <text x={barX + bw + 7} y={y + 4} fill="var(--text)" fontSize="11" fontWeight="600">{bFmt(d.count)}</text>
            <rect x={0} y={y - rowH / 2} width={W} height={rowH} fill="transparent"><title>{`${d.category}: ${bFmt(d.count)} (${Math.round(100 * d.count / total)}%)`}</title></rect>
          </g>;
        })}
      </svg>
    </div>
  );
}

/* ---- method / gradient usage (blue bars) ---- */
function BMethodBars({ methods }) {
  const data = methods.filter(m => m.count >= 5).sort((a, b) => b.count - a.count);
  const W = 560, rowH = 34, H = 12 + data.length * rowH, barX = 150, barMax = W - barX - 52;
  const max = Math.max(1, ...data.map(d => d.count));
  return (
    <div className="card">
      <h3>Gradient / method usage</h3>
      <p style={{ color: 'var(--muted)', fontSize: '.82rem', margin: '0 0 .6rem' }}>Runs by Evosep gradient (samples-per-day), parsed from filenames. "other/unspecified" = washes, blanks and legacy names.</p>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', overflow: 'visible' }}>
        {data.map((d, i) => {
          const y = 6 + i * rowH + rowH / 2, bw = Math.max(barMax * (d.count / max), 2);
          const fr = d.count ? (100 * d.failed / d.count) : 0;
          return <g key={i}>
            <text x={0} y={y + 4} fill="var(--muted)" fontSize="11">{d.method}</text>
            <rect x={barX} y={y - 9} width={bw} height={18} rx="3" fill={BRK.s1} />
            <text x={barX + bw + 7} y={y + 4} fill="var(--text)" fontSize="11" fontWeight="600">{bFmt(d.count)}</text>
            <rect x={0} y={y - rowH / 2} width={W} height={rowH} fill="transparent"><title>{`${d.method}: ${bFmt(d.count)} runs · ${bFmt(d.failed)} failed (${fr.toFixed(1)}%)`}</title></rect>
          </g>;
        })}
      </svg>
    </div>
  );
}

/* ---- recent failures table ---- */
function BFailTable({ rows }) {
  return (
    <div className="card">
      <h3>Most recent failures</h3>
      <p style={{ color: 'var(--muted)', fontSize: '.82rem', margin: '0 0 .6rem' }}>Newest {rows.length} failed / aborted acquisitions with the raw instrument message.</p>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.82rem' }}>
          <thead><tr>{['When', 'Well', 'Sample', 'Cause', 'Message'].map(hh =>
            <th key={hh} style={{ textAlign: 'left', padding: '.45rem .55rem', borderBottom: '1px solid var(--border)', color: 'var(--muted)', fontSize: '.72rem', textTransform: 'uppercase', letterSpacing: '.3px' }}>{hh}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => {
              const sm = (r.fname || '').match(/(?:^|_)([A-Za-z0-9:.+-]+)_S\d+-[A-H]\d/);
              const cc = bCatClass(r.category);
              return <tr key={i}>
                <td style={{ padding: '.45rem .55rem', borderBottom: '1px solid var(--border)', fontFamily: 'ui-monospace,Menlo,monospace', fontSize: '.78rem', color: 'var(--muted)', whiteSpace: 'nowrap' }}>{r.start_date}</td>
                <td style={{ padding: '.45rem .55rem', borderBottom: '1px solid var(--border)', fontFamily: 'ui-monospace,Menlo,monospace', fontSize: '.78rem', color: 'var(--muted)' }}>{r.well || '—'}</td>
                <td style={{ padding: '.45rem .55rem', borderBottom: '1px solid var(--border)' }}>{sm ? sm[1] : '—'}</td>
                <td style={{ padding: '.45rem .55rem', borderBottom: '1px solid var(--border)' }}>
                  <span style={{ display: 'inline-block', fontSize: '.72rem', fontWeight: 600, padding: '.1rem .45rem', borderRadius: '.8rem', background: cc.bg, color: cc.fg, whiteSpace: 'nowrap' }}>{r.category}</span>
                </td>
                <td style={{ padding: '.45rem .55rem', borderBottom: '1px solid var(--border)', fontFamily: 'ui-monospace,Menlo,monospace', fontSize: '.76rem', color: 'var(--muted)' }}>{r.message}</td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
