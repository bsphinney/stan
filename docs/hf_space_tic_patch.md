# HF Space Patch: Add TIC Trace Support

## Changes needed in `app.py` on HF Space `brettsp/stan`

### 1. Add TIC fields to BenchmarkSubmission model

```python
class BenchmarkSubmission(BaseModel):
    # ... existing fields ...
    
    # Identified TIC trace (128 bins of RT vs Ms1.Apex.Area)
    # For Evosep users this enables cross-lab gradient comparison
    tic_rt_bins: list[float] | None = None      # RT bin centers (minutes)
    tic_intensity: list[float] | None = None     # summed signal per bin
```

### 2. Add TIC to the parquet row in submit()

```python
row = {
    # ... existing fields ...
    "tic_rt_bins": [json.dumps(sub.tic_rt_bins) if sub.tic_rt_bins else None],
    "tic_intensity": [json.dumps(sub.tic_intensity) if sub.tic_intensity else None],
}
```

### 3. Add TIC to the parquet schema

```python
schema = pa.schema([
    # ... existing fields ...
    pa.field("tic_rt_bins", pa.string()),      # JSON array
    pa.field("tic_intensity", pa.string()),    # JSON array
])
```

### 4. Add community average TIC endpoint

```python
@app.get("/api/cohorts/{cohort_id}/tic")
async def cohort_tic(cohort_id: str) -> dict:
    """Compute average TIC trace for a cohort (instrument + SPD)."""
    # Read all submissions for this cohort
    # Parse tic_rt_bins and tic_intensity JSON arrays
    # Interpolate to common RT grid
    # Compute median intensity per bin
    # Return: {"cohort_id": ..., "median_tic": {...}, "n_traces": N}
```

### 5. Add TIC overlay to dashboard HTML

Add a new chart section showing:
- Individual TIC traces (thin lines, colored by lab pseudonym)
- Cohort median TIC (thick dashed line)
- Grouped by SPD (tabs or dropdown)
