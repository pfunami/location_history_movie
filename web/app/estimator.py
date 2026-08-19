"""Online render-time estimator.

Every finished job contributes (total_frames, n_points, actual_seconds).
Prediction is ridge-regression over the accumulated log, blended with a
physical prior (startup cost + seconds per frame) so early predictions are
sane and the model sharpens as history grows.
"""
import numpy as np

PRIOR_BASE_S = 20.0       # container/startup/tile warmup
PRIOR_S_PER_FRAME = 1 / 30.0
PRIOR_S_PER_KPOINT = 0.02


def _features(total_frames, n_points):
    return np.array([1.0, total_frames, n_points / 1000.0])


def prior_predict(total_frames, n_points):
    return (PRIOR_BASE_S + total_frames * PRIOR_S_PER_FRAME
            + (n_points / 1000.0) * PRIOR_S_PER_KPOINT)


def predict(history, total_frames, n_points):
    """history: list of (total_frames, n_points, actual_seconds)."""
    prior = prior_predict(total_frames, n_points)
    rows = [r for r in history if r[2] and r[2] > 0]
    if len(rows) < 3:
        return prior
    X = np.array([_features(f, p) for f, p, _ in rows])
    y = np.array([a for _, _, a in rows])
    lam = 1e-6 * len(rows)
    w = np.linalg.solve(X.T @ X + lam * np.eye(X.shape[1]), X.T @ y)
    fitted = float(_features(total_frames, n_points) @ w)
    # blend toward the fit as evidence accumulates
    alpha = min(0.9, len(rows) / 10.0)
    pred = alpha * fitted + (1 - alpha) * prior
    return max(10.0, pred)


def model_info(history):
    rows = [r for r in history if r[2] and r[2] > 0]
    if not rows:
        return {"samples": 0, "s_per_frame": PRIOR_S_PER_FRAME}
    rate = float(np.median([a / max(f, 1) for f, _, a in rows]))
    return {"samples": len(rows), "s_per_frame": rate}
