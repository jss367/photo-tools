/* Grouped prediction decisions — the client half of one server contract.
 *
 * ``/api/predictions/<id>/accept``, ``/replace-keywords`` and
 * ``/accept-subject`` all route through ``accept_prediction``, which fans the
 * decision out across every still-pending row in the same burst group. One
 * request therefore settles several rows, and each of the three routes names
 * every row its transaction decided in the response's ``prediction_ids``.
 *
 * Any caller that loops those routes over a snapshot of rows has to consume
 * that list. Otherwise its next iteration asks the server to decide a row the
 * loop itself already decided, meets the terminal-status 409, and reports it
 * as "not applied" or aborts the run — a false claim about the database, made
 * about work the loop had just performed.
 *
 * The list is also the only version of that answer a caller can trust, which
 * is why it rides on the write instead of being inferred from the 409's
 * "already accepted" wording: the server cannot say *who* settled a row. A row
 * settled from a second tab is not in this list, still 409s, and must still be
 * reported as a refusal. Preserving that distinction is the entire point —
 * "this batch did it" and "somebody else did it" are different facts and the
 * user is owed the difference.
 *
 * It lives in one shared, DOM-free file so every consumer (Review's Accept All
 * and single-card accept, ID Conflicts' batch accept) runs the same
 * implementation — two private copies is exactly how those two pages came to
 * disagree — and so tests can drive it under Node against a stub server.
 */
(function(global) {
  'use strict';

  var Vireo = global.Vireo = global.Vireo || {};

  /* The rows a single decision response says its transaction decided.
   *
   * Absent or malformed ``prediction_ids`` yields an empty list rather than a
   * throw: an older/errored body must not be read as "this decided nothing
   * beyond the row I asked about" *or* crash the loop — callers still credit
   * the row named in the URL themselves.
   */
  function decidedPredictionIds(response) {
    var ids = response && response.prediction_ids;
    return Array.isArray(ids) ? ids : [];
  }

  /* Tracks which rows *this* run has already decided.
   *
   * Usage in a loop over a stale snapshot:
   *
   *   var decided = Vireo.predictions.groupedDecisionTracker();
   *   for (...) {
   *     if (decided.alreadyDecided(row.id)) continue;  // applied, not skipped
   *     decided.record(await post(row.id));
   *   }
   */
  function groupedDecisionTracker() {
    var decided = new Set();
    return {
      alreadyDecided: function(predId) {
        return decided.has(predId);
      },
      // Records what the response says it wrote and returns those ids, so a
      // caller can update its local rows in the same statement.
      record: function(response) {
        var ids = decidedPredictionIds(response);
        for (var i = 0; i < ids.length; i++) decided.add(ids[i]);
        return ids;
      },
      size: function() {
        return decided.size;
      },
    };
  }

  Vireo.predictions = Vireo.predictions || {};
  Vireo.predictions.decidedPredictionIds = decidedPredictionIds;
  Vireo.predictions.groupedDecisionTracker = groupedDecisionTracker;
})(typeof window !== 'undefined' ? window : globalThis);
