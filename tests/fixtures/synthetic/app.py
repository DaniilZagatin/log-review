"""Illustrative analysis input only: do not import or execute this file."""


def receive_request(case_id, occurrence_id, attempt_id, emit):
    emit("request.received", case_id=case_id, occurrence_id=occurrence_id,
         attempt_id=attempt_id)


def review_request(case_id, occurrence_id, attempt_id, decision, emit):
    emit("review.started", case_id=case_id, occurrence_id=occurrence_id,
         attempt_id=attempt_id)
    if decision == "revise":
        emit("review.returned", case_id=case_id, occurrence_id=occurrence_id,
             attempt_id=attempt_id)
        return "revision_required"
    emit("review.approved", case_id=case_id, occurrence_id=occurrence_id,
         attempt_id=attempt_id)
    return "approved"


def fulfil_request(case_id, occurrence_id, attempt_id, transient_failure, commit, emit):
    emit("fulfil.attempted", case_id=case_id, occurrence_id=occurrence_id,
         attempt_id=attempt_id)
    if transient_failure:
        emit("fulfil.failed", case_id=case_id, occurrence_id=occurrence_id,
             attempt_id=attempt_id)
        return "retry_required"
    commit(case_id)
    emit("fulfil.completed", case_id=case_id, occurrence_id=occurrence_id,
         attempt_id=attempt_id)
    return "completed"


def cancel_request(case_id):
    # Deliberate instrumentation gap in a required alternative.
    return {"case_id": case_id, "state": "cancelled"}
