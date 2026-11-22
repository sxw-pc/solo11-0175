"""Ion-composition bounds for intake water parcels.

Electrical conductivity is a total contribution: a reading of EC=1200 is
compatible with Na=900/Ca=300 *and* Na=300/Ca=900.  The synthetic calibration
fluid defines the EC index as ``Na + Ca`` with both ions expressed in the same
unit, so ion analyses can only cross-constrain EC when they belong to the same
water parcel and the same sampling window.  Everything in this module is pure
logic over plain values so the constraints can be tested without a database.
"""

TOL = 1e-6

# Per-reading analytical tolerance for Na+Ca vs drift-corrected EC.  The
# strict single-parameter constraint for a whole calibration batch is enforced
# separately through intersect_intervals; this wider band only rejects
# physically impossible compositions on the reading itself.
ION_TOL = 25.0

# Crop-specific sodium-harm threshold on Na / (Na + Ca).
CROP_THRESHOLDS = {'rice': 0.5}

# EC staging threshold kept for the provisional conductivity-only advice.
EC_HOLD = 1000.0


def ec_provisional(ec_us):
    """The conductivity-only staging state; never a final crop decision."""
    return 'hold' if ec_us >= EC_HOLD else 'suitable'


def batch_drift(standard_ec, observed_ec):
    """One shared baseline-drift parameter per instrument calibration batch.

    The calibration fluid's EC index is the sum of its standard Na and Ca, so
    the drift is a single additive offset applied to every meter of the batch.
    """
    return observed_ec - standard_ec


def corrected_ec(raw_ec, drift):
    return raw_ec - drift


def sodium_ratio(na, ca):
    total = na + ca
    if total <= TOL:
        raise ValueError('empty_ion_total')
    return na / total


def ec_only_range():
    """EC alone leaves the sodium fraction free over the whole simplex."""
    return (0.0, 1.0)


def ion_point_range(na, ca):
    """A same-parcel, same-window ion analysis pins the fraction to a point."""
    ratio = sodium_ratio(na, ca)
    return (ratio, ratio)


def electrode_range(ratios):
    """Conservative bounds from every patrol point covering a segment."""
    if not ratios:
        raise ValueError('empty_patrol')
    return (min(ratios), max(ratios))


def decide(ratio_lo, ratio_hi, crop='rice'):
    """Final suitability from a feasible sodium-fraction interval."""
    threshold = CROP_THRESHOLDS.get(crop, 0.5)
    if ratio_hi <= threshold + TOL:
        return 'suitable'
    if ratio_lo > threshold + TOL:
        return 'forbidden'
    return 'pending'


def ion_drift_interval(raw_ec, na, ca, tol=TOL):
    """Drift values that keep Na+Ca equal to the drift-corrected EC."""
    if na < 0 or ca < 0:
        raise ValueError('negative_ion')
    required = raw_ec - (na + ca)
    return (required - tol, required + tol)


def intersect_intervals(intervals):
    """Joint feasible drift over a whole calibration batch.

    Returns ``None`` when the readings cannot share one drift parameter, i.e.
    accepting them all would produce a release set incompatible with the
    batch's calibration fluid.
    """
    intervals = list(intervals)
    if not intervals:
        raise ValueError('empty_intervals')
    lo = max(i[0] for i in intervals)
    hi = min(i[1] for i in intervals)
    if lo > hi + TOL:
        return None
    return (lo, hi)


def drift_supported(batch_drift_value, joint_interval, tol=TOL):
    """Check the batch's single drift term against a joint feasible interval."""
    if joint_interval is None:
        return False
    return joint_interval[0] - tol <= batch_drift_value <= joint_interval[1] + tol


def ion_matches_ec(raw_ec, na, ca, drift, tol=ION_TOL):
    """Same water parcel, same sampling window: Na+Ca must meet corrected EC."""
    if na < 0 or ca < 0:
        return False
    return abs((na + ca) - corrected_ec(raw_ec, drift)) <= tol


def window_contains(start_at, end_at, at):
    return start_at <= at <= end_at


def windows_overlap(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def window_intersection(a_start, a_end, b_start, b_end):
    lo, hi = max(a_start, b_start), min(a_end, b_end)
    if lo >= hi:
        return None
    return (lo, hi)


def remainder_segments(window_start, window_end, cover_start, cover_end):
    """Parts of a sampling window not reached by electrode patrol coverage."""
    out = []
    if window_start < cover_start:
        out.append((window_start, cover_start))
    if cover_end < window_end:
        out.append((cover_end, window_end))
    return out


def electrode_cross_check(raw_ec, na, drift, tol=TOL):
    """An instantaneous Na electrode point must stay inside the corrected EC.

    Returns the implied Ca and sodium fraction.  A point outside any matching
    parcel's mixing window is rejected by the caller; this only checks the
    shared-batch calibration consistency.
    """
    corrected = corrected_ec(raw_ec, drift)
    if na < -TOL or na > corrected + tol:
        raise ValueError('electrode_ec_crosscheck_failed')
    ca = corrected - na
    if ca < -TOL:
        raise ValueError('electrode_ec_crosscheck_failed')
    return max(ca, 0.0), sodium_ratio(max(na, 0.0), max(ca, 0.0))
