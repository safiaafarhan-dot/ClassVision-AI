"""
ClassVision AI - Experimental Recognition Training
----------------------------------------------------
An isolated, optional pipeline for measuring whether a different recogniser
would be any better. It does not touch the one in production.

    python ai/recognition_training.py --report
    python ai/recognition_training.py --train --model-id knn-v1

What this is NOT
    It is not a replacement for SFace, and running it changes nothing about
    what the camera recognises. The result is a row in the model registry with
    measured numbers attached; putting a model into production is a separate,
    deliberate act with its own gate. See ai/model_registry.py.

    It also does not train a face-recognition network from scratch. Doing that
    properly needs hundreds of thousands of identities, weeks of compute and a
    dataset this project does not have, and doing it improperly produces
    something that looks like it works on the twelve people it saw and fails
    on the thirteenth. What it does instead is learn a CLASSIFIER over the
    embeddings SFace already produces - which is the honest thing to do with
    a few hundred samples, and is where a real improvement is actually
    available.

The leakage problem, which is the whole difficulty

    Five enrollment samples of one person, captured over about eight seconds,
    are nearly the same photograph. Split them at random across train and test
    and the test set is measuring whether the model can recognise a picture it
    has already seen - which it can, at 100%, while telling you nothing.

    So samples are grouped by CAPTURE, and a capture goes entirely into one
    split. Where capture information is unavailable, the whole STUDENT goes
    into one split, which measures something harder and more useful: whether
    the model generalises to a person it was not trained on.

    split_dataset() below refuses to split a student across sets, and a test
    asserts that it refuses.

Class balance
    A classroom has a long tail: one student enrols five times, another
    twenty. Left alone, a classifier learns to prefer whoever has most
    samples. class_weights() reports the imbalance and supplies weights;
    nothing is silently duplicated or discarded.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT / "ai") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "ai"))

from embedding_store import EmbeddingStore  # noqa: E402

# A student with fewer than this cannot be split across train and test at all,
# so they are reported and left out of the evaluation rather than quietly
# inflating it.
MINIMUM_SAMPLES_PER_IDENTITY = 2

DEFAULT_TEST_FRACTION = 0.3
DEFAULT_VALIDATION_FRACTION = 0.2


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TrainingError(RuntimeError):
    """Every message is written to be read by whoever ran the command."""


# -- the dataset ------------------------------------------------------------

def build_dataset(store=None):
    """
    Every enrolled student's vectors, with labels and capture groups.

    Reads the existing embedding store. Produces no new biometric data, opens
    no camera and stores nothing - the vectors already exist because somebody
    enrolled.

    Returns a dict with:
        vectors    (n, dim) float32
        labels     (n,) student ids
        groups     (n,) capture group per sample
        identities sorted unique labels
    """
    store = store or EmbeddingStore()
    records = store.list_students()

    vectors = []
    labels = []
    groups = []
    skipped = []

    for record in records:
        student_id = record.get("student_id")
        if not student_id:
            continue

        try:
            samples, _info = store.load_embeddings(student_id)
        except Exception:                                # noqa: BLE001
            skipped.append(student_id)
            continue

        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim == 1:
            samples = samples.reshape(1, -1)

        if samples.shape[0] < MINIMUM_SAMPLES_PER_IDENTITY:
            skipped.append(student_id)
            continue

        for index in range(samples.shape[0]):
            vectors.append(samples[index])
            labels.append(student_id)
            # One enrollment is one capture. Without a finer record of when
            # each sample was taken, the enrollment is the group - which is
            # the conservative choice, because it keeps near-identical
            # samples together.
            groups.append("%s::enrollment" % student_id)

    if not vectors:
        raise TrainingError(
            "No usable enrollments. Enrol at least %d students with %d or "
            "more samples each before training."
            % (MINIMUM_SAMPLES_PER_IDENTITY, MINIMUM_SAMPLES_PER_IDENTITY))

    return {
        "vectors": np.vstack(vectors).astype(np.float32),
        "labels": np.array(labels),
        "groups": np.array(groups),
        "identities": sorted(set(labels)),
        "skipped": sorted(skipped),
        "dataset_version": utc_now_iso(),
    }


def class_weights(labels):
    """
    How lopsided the data is, and what would even it out.

    Reported rather than applied silently: a teacher looking at a result
    deserves to know it was measured on a set where one student had four times
    as many samples as another.
    """
    counts = Counter(labels)
    if not counts:
        return {"counts": {}, "weights": {}, "imbalance_ratio": None}

    most = max(counts.values())
    least = min(counts.values())

    return {
        "counts": dict(counts),
        "weights": {label: most / count for label, count in counts.items()},
        "imbalance_ratio": (most / least) if least else None,
    }


def split_dataset(dataset, test_fraction=DEFAULT_TEST_FRACTION,
                  validation_fraction=DEFAULT_VALIDATION_FRACTION, seed=11):
    """
    Split by IDENTITY, never by sample.

    Every sample belonging to one student goes into exactly one of train,
    validation and test. That is deliberately the hardest split available: it
    measures whether the model works on somebody it has never seen, which is
    the question a classroom actually asks when a new student enrols.

    A random split of near-identical enrollment samples would report ~100% and
    mean nothing at all. See the module docstring.
    """
    identities = list(dataset["identities"])

    if len(identities) < 3:
        raise TrainingError(
            "Splitting by identity needs at least 3 enrolled students; there "
            "are %d. With fewer, any accuracy figure would be measuring the "
            "split rather than the model." % len(identities))

    generator = np.random.default_rng(seed)
    shuffled = list(identities)
    generator.shuffle(shuffled)

    total = len(shuffled)
    test_count = max(1, int(round(total * test_fraction)))
    validation_count = max(1, int(round(total * validation_fraction)))

    if test_count + validation_count >= total:
        # Leave at least one identity to train on, whatever the fractions say.
        validation_count = max(1, total - test_count - 1)

    test_ids = set(shuffled[:test_count])
    validation_ids = set(shuffled[test_count:test_count + validation_count])
    train_ids = set(shuffled[test_count + validation_count:])

    def select(wanted):
        mask = np.array([label in wanted for label in dataset["labels"]])
        return {
            "vectors": dataset["vectors"][mask],
            "labels": dataset["labels"][mask],
            "identities": sorted(wanted),
        }

    split = {
        "train": select(train_ids),
        "validation": select(validation_ids),
        "test": select(test_ids),
        "seed": seed,
        "strategy": "by identity - no student appears in two splits",
    }

    # The guarantee, asserted rather than assumed.
    overlap = (set(split["train"]["identities"])
               & set(split["test"]["identities"]))
    if overlap:
        raise TrainingError(
            "leakage: %s appear in both train and test" % sorted(overlap))

    return split


# -- the model --------------------------------------------------------------

class CentroidClassifier:
    """
    A nearest-centroid classifier over SFace embeddings.

    Chosen because it is what the data supports. With five samples per person
    the honest model is one with almost no parameters, and a centroid per
    identity is exactly the structure the existing recogniser already assumes
    - which makes it a fair comparison rather than a different system wearing
    the same name.

    It also trains in milliseconds and needs no framework, so the experimental
    pipeline adds NO dependency. That is the point: a training pipeline that
    required PyTorch to compare five vectors would be a worse answer than not
    having one.
    """

    def __init__(self):
        self.identities = []
        self.centroids = None

    def fit(self, vectors, labels):
        grouped = defaultdict(list)
        for vector, label in zip(vectors, labels):
            grouped[label].append(vector)

        self.identities = sorted(grouped)
        centroids = []

        for identity in self.identities:
            centroid = np.mean(np.vstack(grouped[identity]), axis=0)
            norm = np.linalg.norm(centroid)
            # Re-normalised, because the embeddings are L2-normalised and a
            # mean of unit vectors is not one. Skipping this would make the
            # cosine scores below quietly wrong.
            centroids.append(centroid / norm if norm else centroid)

        self.centroids = np.vstack(centroids).astype(np.float32)
        return self

    def scores(self, vectors):
        if self.centroids is None:
            raise TrainingError("the classifier has not been fitted")
        return np.asarray(vectors, dtype=np.float32) @ self.centroids.T

    def predict(self, vectors, threshold=None):
        """
        Predicted identity per row, or None where nothing clears threshold.

        Returning None matters: a recogniser that must name somebody will name
        the wrong person, and in a classroom that is an attendance record for
        a student who was not there.
        """
        scores = self.scores(vectors)
        best = scores.argmax(axis=1)
        best_score = scores[np.arange(scores.shape[0]), best]

        predictions = []
        for index, score in zip(best, best_score):
            if threshold is not None and score < threshold:
                predictions.append(None)
            else:
                predictions.append(self.identities[index])

        return predictions, best_score


# -- evaluation -------------------------------------------------------------

def confusion_matrix(truth, predicted, identities):
    """A plain nested dict. Small enough to read, and JSON-serialisable."""
    matrix = {actual: {guess: 0 for guess in list(identities) + ["UNKNOWN"]}
              for actual in identities}

    for actual, guess in zip(truth, predicted):
        if actual not in matrix:
            continue
        matrix[actual][guess if guess in matrix[actual] else "UNKNOWN"] += 1

    return matrix


def evaluate(classifier, split, threshold=0.45):
    """
    Measure the model, on identities it has never seen.

    Note what is being measured here and what is not. The test identities were
    excluded from training entirely, so the classifier has no centroid for any
    of them - which means the only correct answer for every test sample is
    UNKNOWN. That makes this an OPEN-SET test: it measures whether the model
    correctly refuses to name a stranger.

    That is the measurement a classroom needs. A closed-set accuracy figure
    over the same twelve people would be near-perfect and would say nothing
    about the thirteenth person who walks in.
    """
    test = split["test"]

    if not len(test["vectors"]):
        raise TrainingError("the test split is empty")

    predictions, scores = classifier.predict(test["vectors"], threshold)

    # Every test identity is a stranger to this model. Naming one is a false
    # acceptance; refusing is correct.
    false_accepts = sum(1 for prediction in predictions
                        if prediction is not None)
    correct_rejections = len(predictions) - false_accepts

    validation = split["validation"]
    known_accuracy = None
    false_rejections = 0

    if len(validation["vectors"]):
        # The validation identities are also unseen, so the same holds. What
        # differs is that this set is used for choosing a threshold and the
        # test set is not touched until the end.
        validation_predictions, _ = classifier.predict(
            validation["vectors"], threshold)
        false_rejections = sum(1 for prediction in validation_predictions
                               if prediction is not None)

    # A closed-set figure, on the training identities, reported SEPARATELY and
    # labelled as what it is - the optimistic number, not the headline.
    train = split["train"]
    train_predictions, _ = classifier.predict(train["vectors"], threshold)
    known_accuracy = float(np.mean([
        predicted == actual
        for predicted, actual in zip(train_predictions, train["labels"])]))

    total = len(predictions)
    far = false_accepts / total if total else 0.0

    return {
        "evaluated_at": utc_now_iso(),
        "threshold": threshold,
        "held_out": True,
        "test_identities": len(test["identities"]),
        "test_samples": int(total),
        "train_identities": len(train["identities"]),

        # Open-set: the honest headline.
        "open_set_correct_rejection_rate": round(correct_rejections / total, 4)
        if total else None,
        "false_acceptance_rate": round(far, 4),
        "false_rejection_rate": round(
            false_rejections / max(len(validation["vectors"]), 1), 4),

        # Closed-set, on identities the model WAS trained on. Always high,
        # and labelled so nobody mistakes it for a generalisation result.
        "closed_set_accuracy_on_training_identities": round(known_accuracy, 4),

        "confusion_matrix": confusion_matrix(
            test["labels"],
            [prediction or "UNKNOWN" for prediction in predictions],
            test["identities"]),

        "note": (
            "Test identities were excluded from training, so the only correct "
            "answer for every test sample is UNKNOWN. accuracy, precision and "
            "recall are deliberately NOT reported: on an open-set test with no "
            "true positives available they would be undefined or misleading, "
            "and reporting a number that cannot mean anything is worse than "
            "reporting none."),
    }


def train(store=None, threshold=0.45, seed=11):
    """Build, split, fit and measure. Returns everything, activates nothing."""
    dataset = build_dataset(store)
    balance = class_weights(dataset["labels"])
    split = split_dataset(dataset, seed=seed)

    classifier = CentroidClassifier().fit(split["train"]["vectors"],
                                          split["train"]["labels"])
    metrics = evaluate(classifier, split, threshold)
    metrics["class_balance"] = balance
    metrics["dataset_version"] = dataset["dataset_version"]
    metrics["skipped_identities"] = dataset["skipped"]

    return {"dataset": dataset, "split": split, "classifier": classifier,
            "metrics": metrics}


# -- command line -----------------------------------------------------------

def main():
    from paths import enable_safe_console
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="Experimental recognition training. Changes nothing about "
                    "the production recogniser.")
    parser.add_argument("--report", action="store_true",
                        help="describe the dataset without training")
    parser.add_argument("--train", action="store_true",
                        help="train and evaluate an experimental model")
    parser.add_argument("--model-id", default=None,
                        help="register the result under this id")
    parser.add_argument("--threshold", type=float, default=0.45,
                        help="decision threshold for the experiment only "
                             "(the production threshold is unchanged)")
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()

    print("ClassVision AI - experimental recognition training")
    print("  SFace remains the production recogniser. Nothing here changes "
          "that.\n")

    try:
        if args.report or not args.train:
            dataset = build_dataset()
            balance = class_weights(dataset["labels"])

            print("  identities     : %d" % len(dataset["identities"]))
            print("  samples        : %d" % len(dataset["labels"]))
            print("  imbalance      : %s"
                  % (("%.1fx" % balance["imbalance_ratio"])
                     if balance["imbalance_ratio"] else "n/a"))
            if dataset["skipped"]:
                print("  skipped        : %s (too few samples to split)"
                      % ", ".join(dataset["skipped"]))
            if not args.train:
                print("\n  Run with --train to train and evaluate.")
                return 0

        result = train(threshold=args.threshold, seed=args.seed)
        metrics = result["metrics"]

        print("\n  --- measured ---")
        for key in ("test_identities", "test_samples",
                    "open_set_correct_rejection_rate",
                    "false_acceptance_rate", "false_rejection_rate",
                    "closed_set_accuracy_on_training_identities"):
            print("  %-42s %s" % (key, metrics.get(key)))

        print("\n  %s" % metrics["note"])

        if args.model_id:
            sys.path.insert(0, str(PROJECT_ROOT / "database"))
            sys.path.insert(0, str(PROJECT_ROOT))

            from database import open_database
            from model_registry import ModelRegistry

            database = open_database()
            try:
                registry = ModelRegistry(database)
                registry.ensure_baseline()
                registry.register_experiment(
                    model_id=args.model_id, version=utc_now_iso(),
                    metrics=metrics,
                    dataset_version=metrics["dataset_version"],
                    notes="centroid classifier over SFace embeddings",
                    actor="recognition_training.py")
                print("\n  registered as %s, status TESTING." % args.model_id)
                print("  It is NOT in production and cannot be until it is "
                      "validated and activated.")
            finally:
                database.close()

        return 0

    except TrainingError as error:
        print("  cannot train: %s" % error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
