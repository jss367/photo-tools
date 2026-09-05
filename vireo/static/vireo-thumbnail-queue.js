/* Browse thumbnails share the browser's connections with interactive API calls.
 * Keep only two downloads in flight, and discard work outside a small runway.
 * Aborting a fetch frees the client connection; it cannot undo generation that
 * the server has already started. Normal HTTP caching still applies to fetch.
 */
(function() {
  'use strict';
  var root = document.getElementById('gridContainer');
  var grid = document.getElementById('grid');
  if (!root || !grid) return;

  var selector = 'img[data-thumbnail-src]';
  var states = new Map();
  var nearby = new Set();
  var active = 0;
  var frame = null;
  var margin = 200;

  function schedule() {
    if (frame === null) frame = requestAnimationFrame(pump);
  }

  function cancel(state) {
    if (state.controller) state.controller.abort();
    state.controller = null;
  }

  function release(state) {
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = null;
    state.loaded = false;
    state.img.removeAttribute('src');
  }

  function leave(state) {
    nearby.delete(state);
    cancel(state);
    state.failed = false; // Retry a failed image when the user returns to it.
    state.img.dispatchEvent(new Event('vireo:thumbnail-cancelled'));
  }

  function sync(img) {
    var state = states.get(img);
    if (!grid.contains(img) || !img.matches(selector)) {
      if (state) {
        leave(state);
        release(state);
        observer.unobserve(img);
        states.delete(img);
      }
      return;
    }
    var url = img.getAttribute('data-thumbnail-src');
    if (!state) {
      state = {img: img, url: url};
      states.set(img, state);
      observer.observe(img);
    } else if (state.url !== url) {
      cancel(state);
      release(state);
      state.url = url;
      state.failed = false;
    }
    schedule();
  }

  async function download(state) {
    var controller = new AbortController();
    state.controller = controller;
    active++;
    try {
      var response = await fetch(state.url, {
        signal: controller.signal,
        credentials: 'same-origin'
      });
      if (!response.ok) throw new Error('Thumbnail HTTP ' + response.status);
      var blob = await response.blob();
      // Aborted responses and a previous edit's pixels must never land on a
      // recycled card, even if the response completed just before abort().
      if (state.controller !== controller || !grid.contains(state.img)) return;
      state.objectUrl = URL.createObjectURL(blob);
      state.loaded = true;
      state.img.src = state.objectUrl;
    } catch (error) {
      if (state.controller === controller && !controller.signal.aborted) {
        state.failed = true;
        state.img.dispatchEvent(new Event('error'));
      }
    } finally {
      if (state.controller === controller) state.controller = null;
      // Keep the slot until fetch/body consumption has actually settled.
      active--;
      schedule();
    }
  }

  function pump() {
    frame = null;
    var bounds = root.getBoundingClientRect();
    var candidates = [];
    nearby.forEach(function(state) {
      var rect = state.img.getBoundingClientRect();
      var distance = Math.max(bounds.top - rect.bottom, rect.top - bounds.bottom, 0);
      if (!grid.contains(state.img) || !rect.width || !rect.height || distance > margin) {
        leave(state);
        return;
      }
      if (!state.loaded && !state.failed && !state.controller && state.url) {
        candidates.push({
          state: state, distance: distance,
          visible: rect.bottom > bounds.top && rect.top < bounds.bottom
        });
      }
    });
    // Photos actually visible win over prefetches above/below the viewport.
    candidates.sort(function(a, b) {
      return Number(b.visible) - Number(a.visible) || a.distance - b.distance;
    });
    for (var i = 0; i < candidates.length && active < 2; i++) {
      download(candidates[i].state);
    }
  }

  var observer = new IntersectionObserver(function(entries) {
    entries.forEach(function(entry) {
      var state = states.get(entry.target);
      if (!state) return;
      if (entry.isIntersecting) nearby.add(state);
      else leave(state);
    });
    schedule();
  }, {root: root, rootMargin: margin + 'px 0px'});

  function visit(node) {
    if (node.nodeType !== 1) return;
    if (node.matches(selector)) sync(node);
    node.querySelectorAll(selector).forEach(sync);
  }

  // Covers appended pages, full grid replacement, stack trays and edits,
  // without rescanning every loaded card on each sidebar or badge update.
  new MutationObserver(function(records) {
    records.forEach(function(record) {
      if (record.type === 'attributes') sync(record.target);
      else {
        record.removedNodes.forEach(visit);
        record.addedNodes.forEach(visit);
      }
    });
    schedule();
  }).observe(grid, {
    childList: true, subtree: true,
    attributes: true, attributeFilter: ['data-thumbnail-src']
  });
  visit(grid);
  root.addEventListener('scroll', schedule, {passive: true});
  window.addEventListener('resize', schedule);
})();
