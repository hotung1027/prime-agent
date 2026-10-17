# batch - offline batch submission

`batch.py` submits many independent items as one batch job and polls for the result.

Gate: batch is only chosen when the work is single-turn, non-blocking, large enough to save money,
and latency does not matter. A pipeline stage never waits on a batch; `reflect.py` is the intended
caller.

    import batch
    job = batch.submit(items, model="...")
    res = batch.collect(job["id"])
