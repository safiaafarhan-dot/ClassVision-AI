"""
ClassVision AI - Recognition Model Registry
---------------------------------------------
Which model is in production, which is being tested, and how one moves.

The one rule

    SFace is the production baseline. It got there by being a published,
    evaluated model with a known provenance, and it stays there until
    something else has been measured against a real held-out set and a person
    has deliberately activated it.

    Nothing in this file can promote a model on its own. Training writes
    metrics; a human writes ACTIVE.

The states, and what each one actually means

    BASELINE    SFace. The production recogniser. Always present, and there
                is no operation in this module that can remove it.

    TRAINING    A run is in progress. It has no metrics yet and could not be
                activated if somebody tried.

    TESTING     Trained, with metrics, on data the author chose. Not
                independently checked. This is where an experiment lives, and
                most experiments should stay here.

    VALIDATED   Evaluated on a held-out set that the training never saw, and
                the numbers cleared the activation gate. Eligible for
                production; still not in it.

    ACTIVE      In production. Exactly one model is, ever.

    RETIRED     Was ACTIVE, is not any more. Kept so a rollback has something
                to roll back to, and so an attendance record produced under
                it can still say which model produced it.

Why activation has a gate rather than a button

    A face recogniser that is slightly worse than the one it replaced does
    not announce itself. It marks a handful of students absent who were
    present, spread over weeks, and by the time somebody notices, the
    register is wrong and nobody knows which entries to trust.

    So ACTIVATION_GATE below is a floor, checked against measured numbers,
    and refusing is the default. The numbers are conservative on purpose: a
    replacement for a working system should have to be clearly better, not
    arguably comparable.
"""

import json
from datetime import datetime, timezone

# The production baseline. Named here so nothing else has to know the string,
# and so a test can assert it never moves.
BASELINE_MODEL_ID = "sface-baseline"
BASELINE_MODEL_NAME = "sface"
BASELINE_VERSION = "2021dec"

STATUS_BASELINE = "BASELINE"
STATUS_TRAINING = "TRAINING"
STATUS_TESTING = "TESTING"
STATUS_VALIDATED = "VALIDATED"
STATUS_ACTIVE = "ACTIVE"
STATUS_RETIRED = "RETIRED"

ALL_STATUSES = (STATUS_BASELINE, STATUS_TRAINING, STATUS_TESTING,
                STATUS_VALIDATED, STATUS_ACTIVE, STATUS_RETIRED)

KIND_BASELINE = "baseline"
KIND_EXPERIMENTAL = "experimental"

# What a custom model must MEASURE before it may be validated. A floor, not a
# target, and deliberately strict: this is the bar for replacing a recogniser
# that already works in a real classroom.
ACTIVATION_GATE = {
    "accuracy": 0.98,
    "precision": 0.98,
    "recall": 0.98,
    # False acceptance is the one that matters most: a stranger recognised as
    # a student is an attendance record for somebody who was not there.
    "false_acceptance_rate": 0.005,
    "false_rejection_rate": 0.05,
    # Measured on identities the training never saw. A model evaluated only on
    # the students it was trained on has proved nothing about tomorrow's
    # intake.
    "minimum_test_identities": 5,
    "minimum_test_samples": 50,
}


class ModelRegistryError(ValueError):
    """Every message is written to be read by a person making a decision."""


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ModelRegistry:
    """The registry, and the rules about moving between its states."""

    def __init__(self, database):
        from repositories import AuditRepository, ModelRepository

        self.database = database
        self.models = ModelRepository(database)
        self.audit = AuditRepository(database)

    # -- the baseline ------------------------------------------------------

    def ensure_baseline(self):
        """
        Make sure SFace is recorded, and is the active model.

        Idempotent, and safe to call on every start. It never demotes a model
        somebody deliberately activated - if a validated experiment is ACTIVE,
        this leaves it alone and simply makes sure the baseline still exists
        to roll back to.
        """
        existing = self.models.get(BASELINE_MODEL_ID)

        if existing is None:
            self.models.register(
                model_id=BASELINE_MODEL_ID,
                model_name=BASELINE_MODEL_NAME,
                version=BASELINE_VERSION,
                kind=KIND_BASELINE,
                status=STATUS_BASELINE,
                notes="The production recogniser. Published, evaluated, and "
                      "the model every attendance record so far was produced "
                      "with.")
            existing = self.models.get(BASELINE_MODEL_ID)

        if self.models.active() is None:
            self.models.set_status(BASELINE_MODEL_ID, STATUS_ACTIVE,
                                   activated_at=utc_now_iso())

        return self.models.get(BASELINE_MODEL_ID)

    def active(self):
        return self.models.active() or self.ensure_baseline()

    def list_all(self):
        return self.models.list_all()

    def get(self, model_id):
        return self.models.get(model_id)

    # -- experiments -------------------------------------------------------

    def register_experiment(self, model_id, version, metrics=None,
                            dataset_version=None, notes=None, actor=None):
        """
        Record a trained experiment. It lands in TESTING, never higher.

        Registering is not a claim that a model is any good - it is a record
        that somebody trained one and what it measured.
        """
        if model_id == BASELINE_MODEL_ID:
            raise ModelRegistryError(
                "%s is the production baseline and cannot be overwritten by "
                "an experiment." % BASELINE_MODEL_ID)

        record = self.models.register(
            model_id=model_id,
            model_name=model_id,
            version=version,
            kind=KIND_EXPERIMENTAL,
            status=STATUS_TESTING,
            dataset_version=dataset_version,
            metrics=metrics,
            notes=notes,
            trained_at=utc_now_iso())

        self.audit.record("model.registered", actor=actor, subject=model_id,
                          detail="version %s" % version)
        return record

    def evaluate_gate(self, metrics):
        """
        Whether measured numbers clear the activation floor, and why not.

        Returns (ok, reasons). A missing metric is a FAILURE, not a pass: a
        model that did not measure its false-acceptance rate has not shown it
        is safe, and treating silence as success is how a worse recogniser
        gets promoted.
        """
        reasons = []
        metrics = metrics or {}

        for field in ("accuracy", "precision", "recall"):
            value = metrics.get(field)
            if value is None:
                reasons.append("%s was not measured" % field)
            elif float(value) < ACTIVATION_GATE[field]:
                reasons.append("%s is %.4f, below the required %.4f"
                               % (field, float(value), ACTIVATION_GATE[field]))

        for field in ("false_acceptance_rate", "false_rejection_rate"):
            value = metrics.get(field)
            if value is None:
                reasons.append("%s was not measured" % field)
            elif float(value) > ACTIVATION_GATE[field]:
                reasons.append("%s is %.4f, above the permitted %.4f"
                               % (field, float(value), ACTIVATION_GATE[field]))

        identities = metrics.get("test_identities")
        if identities is None:
            reasons.append("the number of test identities was not reported")
        elif int(identities) < ACTIVATION_GATE["minimum_test_identities"]:
            reasons.append(
                "evaluated on %d identities; at least %d are required for the "
                "result to mean anything"
                % (int(identities), ACTIVATION_GATE["minimum_test_identities"]))

        samples = metrics.get("test_samples")
        if samples is None:
            reasons.append("the number of test samples was not reported")
        elif int(samples) < ACTIVATION_GATE["minimum_test_samples"]:
            reasons.append("evaluated on %d samples; at least %d are required"
                           % (int(samples),
                              ACTIVATION_GATE["minimum_test_samples"]))

        if metrics.get("held_out") is not True:
            reasons.append(
                "the evaluation set was not declared held-out - a model "
                "measured on its own training data has proved nothing")

        return (not reasons), reasons

    def validate(self, model_id, metrics=None, actor=None):
        """
        Move an experiment to VALIDATED, or explain why it cannot.

        Still not production. Validation says the numbers are good enough to
        CONSIDER; activation is a separate, deliberate act.
        """
        record = self.models.get(model_id)
        if record is None:
            raise ModelRegistryError("No model called %s." % model_id)

        if record["kind"] == KIND_BASELINE:
            raise ModelRegistryError(
                "The baseline does not need validating.")

        measured = metrics if metrics is not None else record.get("metrics")
        ok, reasons = self.evaluate_gate(measured)

        if not ok:
            raise ModelRegistryError(
                "%s does not meet the activation requirements:\n  - %s"
                % (model_id, "\n  - ".join(reasons)))

        if metrics is not None:
            self.models.register(
                model_id=model_id, model_name=record["model_name"],
                version=record["version"], kind=record["kind"],
                status=STATUS_VALIDATED, metrics=metrics,
                dataset_version=record.get("dataset_version"),
                notes=record.get("notes"), trained_at=record.get("trained_at"))
        else:
            self.models.set_status(model_id, STATUS_VALIDATED)

        self.audit.record("model.validated", actor=actor, subject=model_id)
        return self.models.get(model_id)

    def activate(self, model_id, actor=None, confirm=None):
        """
        Put a model into production. The only thing that changes recognition.

        Refused for anything that is not VALIDATED, and refused again unless
        the caller types the model id - the same protection a permanent
        deletion gets, because promoting an unproven recogniser silently
        corrupts a register in exactly the same way.

        The model it replaces becomes RETIRED rather than disappearing, so
        rollback is one call and so a past attendance record can still say
        what produced it.
        """
        record = self.models.get(model_id)
        if record is None:
            raise ModelRegistryError("No model called %s." % model_id)

        if record["status"] == STATUS_ACTIVE:
            return record

        if record["kind"] != KIND_BASELINE and \
                record["status"] != STATUS_VALIDATED:
            raise ModelRegistryError(
                "%s is %s. Only a VALIDATED model may be activated - validate "
                "it against a held-out set first." % (model_id,
                                                      record["status"]))

        if (confirm or "").strip() != model_id:
            raise ModelRegistryError(
                "To put %s into production, type its model id exactly. This "
                "changes what the camera recognises." % model_id)

        previous = self.models.active()

        with self.database.transaction():
            if previous and previous["model_id"] != model_id:
                self.models.set_status(previous["model_id"], STATUS_RETIRED)
            self.models.deactivate_others(model_id)
            self.models.set_status(model_id, STATUS_ACTIVE,
                                   activated_at=utc_now_iso())

        self.audit.record(
            "model.activated", actor=actor, subject=model_id,
            detail="replaced %s" % (previous or {}).get("model_id", "nothing"))

        return self.models.get(model_id)

    def rollback(self, actor=None):
        """
        Put the baseline back. Always available, never gated.

        Deliberately asymmetric with activate(): promoting an experiment takes
        a validation and a typed confirmation, and undoing it takes one call.
        When something is going wrong in a live classroom, the safe direction
        should be the easy one.
        """
        self.ensure_baseline()
        previous = self.models.active()

        with self.database.transaction():
            if previous and previous["model_id"] != BASELINE_MODEL_ID:
                self.models.set_status(previous["model_id"], STATUS_RETIRED)
            self.models.deactivate_others(BASELINE_MODEL_ID)
            self.models.set_status(BASELINE_MODEL_ID, STATUS_ACTIVE,
                                   activated_at=utc_now_iso())

        self.audit.record(
            "model.rollback", actor=actor, subject=BASELINE_MODEL_ID,
            detail="rolled back from %s"
                   % (previous or {}).get("model_id", "nothing"))

        return self.models.get(BASELINE_MODEL_ID)

    def describe(self):
        """A summary for the dashboard, with the disclaimer attached."""
        active = self.active()

        return {
            "active": active,
            "models": self.list_all(),
            "gate": dict(ACTIVATION_GATE),
            "note": (
                "SFace is the production recognition baseline. A custom model "
                "is experimental until it has been evaluated on a held-out "
                "set and explicitly activated; registering or training one "
                "changes nothing about what the camera recognises."),
        }
