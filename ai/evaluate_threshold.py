"""
ClassVision AI - Threshold Evaluation (Step 4)
----------------------------------------------
Measures how well the recogniser separates "same person" from "different
person", so a recognition threshold can be CHOSEN FROM DATA instead of
guessed.

Why this tool exists
    A threshold is the single most consequential number in the system. Too
    low and strangers are accepted as students; too high and real students
    are never recognised. Picking it by intuition is how face systems end up
    quietly wrong.

What it needs
    A folder of labelled images, one sub-folder per person, at least two
    images each:

        data/eval/
          rahul/   img1.jpg  img2.jpg  img3.jpg
          priya/   img1.jpg  img2.jpg
          ...

    Those images must be real, separate captures - different moments, not
    edited copies of one photo. Edited copies score far too similar and would
    produce a threshold that fails in practice.

What it reports
    - genuine pairs    : two images of the SAME person
    - impostor pairs   : two images of DIFFERENT people
    - the score distribution of each
    - false accepts and false rejects at a range of thresholds
    - the equal-error point, where both mistakes are equally likely

Usage
    python ai/evaluate_threshold.py
    python ai/evaluate_threshold.py --directory path/to/labelled/images

Privacy
    Reads images, reports numbers. It writes nothing, stores nothing, and
    never prints an embedding.
"""

import argparse
import sys
from itertools import combinations
from pathlib import Path

import numpy as np

from face_detector import YuNetFaceDetector
from face_embedding import SFaceEmbedder, cosine_similarity
from face_recognizer import (
    DEFAULT_RECOGNITION_THRESHOLD,
    UPSTREAM_SFACE_COSINE_THRESHOLD,
)
from paths import PROJECT_ROOT, enable_safe_console, relative

DEFAULT_EVAL_DIR = PROJECT_ROOT / "data" / "eval"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# Thresholds to report the error rates at.
REPORT_THRESHOLDS = (0.20, 0.30, 0.363, 0.40, 0.45, 0.50, 0.60, 0.70, 0.80, 0.90)


def load_image(path):
    """Read an image without handing a path to OpenCV (non-ASCII safe)."""
    import cv2
    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def collect_embeddings(directory, detector, embedder):
    """
    Turn every labelled image into an embedding.

    Returns {person: [embedding, ...]} and a list of problems found.
    """
    people = {}
    problems = []

    for person_dir in sorted(p for p in directory.iterdir() if p.is_dir()):
        images = sorted(p for p in person_dir.iterdir()
                        if p.suffix.lower() in IMAGE_SUFFIXES)

        for image_path in images:
            frame = load_image(image_path)
            if frame is None:
                problems.append("%s: could not be read" % relative(image_path))
                continue

            detections = detector.detect(frame)
            if len(detections) != 1:
                problems.append("%s: %d faces found, need exactly 1"
                                % (relative(image_path), len(detections)))
                continue

            try:
                embedding = embedder.embed_detection(frame, detections[0])
            except ValueError as error:
                problems.append("%s: %s" % (relative(image_path), error))
                continue

            people.setdefault(person_dir.name, []).append(embedding)

    return people, problems


def build_pairs(people):
    """Every within-person pair (genuine) and across-person pair (impostor)."""
    genuine = []
    impostor = []

    for person, embeddings in people.items():
        for first, second in combinations(range(len(embeddings)), 2):
            genuine.append((person, cosine_similarity(embeddings[first],
                                                      embeddings[second])))

    names = sorted(people)
    for first_name, second_name in combinations(names, 2):
        for a in people[first_name]:
            for b in people[second_name]:
                impostor.append(((first_name, second_name), cosine_similarity(a, b)))

    return genuine, impostor


def describe(scores, label):
    """Print the distribution of one set of scores."""
    if not scores:
        print("  %-9s none" % label)
        return

    values = np.array(scores, dtype=np.float64)
    print("  %-9s n=%-5d min %.4f  p05 %.4f  median %.4f  p95 %.4f  max %.4f"
          % (label, values.size, values.min(), np.percentile(values, 5),
             np.median(values), np.percentile(values, 95), values.max()))


def error_rates(genuine_scores, impostor_scores, threshold):
    """
    False accept = an impostor pair scoring at or above the threshold.
    False reject = a genuine pair scoring below it.
    """
    false_accepts = sum(1 for s in impostor_scores if s >= threshold)
    false_rejects = sum(1 for s in genuine_scores if s < threshold)

    far = false_accepts / len(impostor_scores) if impostor_scores else float("nan")
    frr = false_rejects / len(genuine_scores) if genuine_scores else float("nan")

    return false_accepts, false_rejects, far, frr


def equal_error_threshold(genuine_scores, impostor_scores):
    """Find the threshold where both error rates are closest to equal."""
    if not genuine_scores or not impostor_scores:
        return None

    candidates = np.unique(np.concatenate([
        np.array(genuine_scores), np.array(impostor_scores)]))

    best = None
    for threshold in candidates:
        _, _, far, frr = error_rates(genuine_scores, impostor_scores, threshold)
        gap = abs(far - frr)
        if best is None or gap < best[1]:
            best = (float(threshold), gap, far, frr)

    return best


def report(people, genuine, impostor, problems):
    """Print the whole evaluation."""
    genuine_scores = [score for _, score in genuine]
    impostor_scores = [score for _, score in impostor]

    print("=" * 74)
    print("THRESHOLD EVALUATION")
    print("=" * 74)
    print("  identities        : %d" % len(people))
    print("  images used       : %d" % sum(len(v) for v in people.values()))
    print("  genuine pairs     : %d" % len(genuine_scores))
    print("  impostor pairs    : %d" % len(impostor_scores))

    if problems:
        print("\n  images skipped    : %d" % len(problems))
        for problem in problems[:10]:
            print("      %s" % problem)
        if len(problems) > 10:
            print("      ... and %d more" % (len(problems) - 10))

    print("\nScore distributions")
    describe(genuine_scores, "genuine")
    describe(impostor_scores, "impostor")

    if genuine_scores and impostor_scores:
        gap = min(genuine_scores) - max(impostor_scores)
        print("\n  lowest genuine  %.4f" % min(genuine_scores))
        print("  highest impostor %.4f" % max(impostor_scores))
        print("  separation       %+.4f %s" % (
            gap, "(clean)" if gap > 0 else "(OVERLAP - the sets are not separable)"))

    print("\nError rates by threshold")
    print("  %-10s %12s %12s %10s %10s" %
          ("threshold", "false accept", "false reject", "FAR", "FRR"))
    print("  " + "-" * 58)
    for threshold in REPORT_THRESHOLDS:
        fa, fr, far, frr = error_rates(genuine_scores, impostor_scores, threshold)
        marker = ""
        if abs(threshold - UPSTREAM_SFACE_COSINE_THRESHOLD) < 1e-9:
            marker = "  <- model authors' published value"
        elif abs(threshold - DEFAULT_RECOGNITION_THRESHOLD) < 1e-9:
            marker = "  <- this project's provisional default"
        print("  %-10.3f %12d %12d %9.2f%% %9.2f%%%s"
              % (threshold, fa, fr, far * 100, frr * 100, marker))

    equal_error = equal_error_threshold(genuine_scores, impostor_scores)
    if equal_error:
        threshold, _, far, frr = equal_error
        print("\n  equal-error point: threshold %.4f (FAR %.2f%%, FRR %.2f%%)"
              % (threshold, far * 100, frr * 100))

    # Concrete examples, which are far more useful than rates alone.
    if impostor_scores:
        worst = sorted(impostor, key=lambda item: -item[1])[:3]
        print("\n  Closest impostor pairs (most likely false accepts):")
        for (a, b), score in worst:
            print("      %-14s vs %-14s %.4f" % (a, b, score))

    if genuine_scores:
        worst = sorted(genuine, key=lambda item: item[1])[:3]
        print("\n  Weakest genuine pairs (most likely false rejects):")
        for person, score in worst:
            print("      %-30s %.4f" % (person, score))


def verdict(people, genuine, impostor):
    """Say plainly whether this data can justify a production threshold."""
    print("\n" + "=" * 74)
    print("VERDICT")
    print("=" * 74)

    identities = len(people)
    genuine_count = len(genuine)
    impostor_count = len(impostor)

    # These minimums are a judgement call about what is obviously too little,
    # not a statistical standard. Real biometric evaluation uses far more.
    enough = identities >= 10 and genuine_count >= 100 and impostor_count >= 1000

    if enough:
        print("  This dataset is large enough to propose a threshold.")
        print("  Review the error rates above and choose the operating point")
        print("  that suits the cost of each mistake in your deployment.")
    else:
        print("  NOT ENOUGH DATA TO SET A PRODUCTION THRESHOLD.")
        print("")
        print("    identities    %3d  (want 10 or more)" % identities)
        print("    genuine pairs %3d  (want 100 or more)" % genuine_count)
        print("    impostor pairs%4d  (want 1000 or more)" % impostor_count)
        print("")
        print("  Any threshold derived from this little data is provisional.")
        print("  The project default stays at %.2f, which is a DEVELOPMENT"
              % DEFAULT_RECOGNITION_THRESHOLD)
        print("  value, not a validated one.")

    print("")
    print("  Reminder: these are similarity measurements, not an accuracy")
    print("  figure. Accuracy would require a labelled benchmark and a")
    print("  protocol this project does not have.")


def main():
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="Measure genuine vs impostor similarity to choose a "
                    "recognition threshold from data.")
    parser.add_argument("--directory", default=str(DEFAULT_EVAL_DIR),
                        help="folder of labelled images (default: data/eval)")
    args = parser.parse_args()

    directory = Path(args.directory)

    if not directory.exists():
        print("No evaluation data found at %s" % relative(directory))
        print("")
        print("Create it as one folder per person, two or more real photos each:")
        print("    data/eval/rahul/1.jpg")
        print("    data/eval/rahul/2.jpg")
        print("    data/eval/priya/1.jpg")
        print("")
        print("They must be separate captures, not edited copies of one photo.")
        print("data/ is gitignored, so nothing you put there can be committed.")
        return 1

    try:
        detector = YuNetFaceDetector()
        embedder = SFaceEmbedder()
    except FileNotFoundError as error:
        print("ERROR: %s" % error)
        return 1

    people, problems = collect_embeddings(directory, detector, embedder)

    usable = {name: vectors for name, vectors in people.items() if vectors}
    if not usable:
        print("No usable face images found in %s" % relative(directory))
        for problem in problems[:10]:
            print("  %s" % problem)
        return 1

    genuine, impostor = build_pairs(usable)
    report(usable, genuine, impostor, problems)
    verdict(usable, genuine, impostor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
