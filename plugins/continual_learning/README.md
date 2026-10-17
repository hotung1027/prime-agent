# continual_learning - daily reflection

`reflect.py` turns a day of recorded traces into a written reflection: it gathers the day's events,
submits them as one batch job to a reasoning model, collects the result, and writes a dated note.

    import reflect
    reflect.tick()            # gather + submit if the day has not been reflected yet
    reflect.collect()         # write the note when the batch settles

Depends on `batch`, `decision_layer`, `engine` and `trace`.
