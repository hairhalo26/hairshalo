/* Google Maps address picker — shared by checkout (index.html) and the
   address book (account.html).

   What it does: a place search that fills the typed address fields, and a
   small map whose pin the customer can drop or drag onto their door. What it
   never does: decide the address. The typed fields stay the address (the
   server validates them); the pin is an extra the courier can use.

   Off unless the backend hands out a key (GET /api/orders/maps-config, from
   the GOOGLE_MAPS_API_KEY environment variable). With no key, or if Google
   cannot be reached or refuses the key, mount() resolves to null and the
   page is exactly what it was before. */
(function () {
  'use strict';

  var configPromise = null;
  var loadPromise = null;
  var failed = false;
  var INDIA = { lat: 20.5937, lng: 78.9629 };

  function config(apiBase) {
    if (!configPromise) {
      configPromise = fetch(apiBase + '/orders/maps-config', { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (c) { return c && c.api_key ? c : null; })
        .catch(function () { configPromise = null; return null; });
    }
    return configPromise;
  }

  function load(cfg) {
    if (window.google && google.maps && google.maps.importLibrary) return Promise.resolve();
    if (!loadPromise) {
      loadPromise = new Promise(function (resolve, reject) {
        window.__hsMapsReady = function () { resolve(); };
        // Google calls this when the key is refused (wrong referrer, API not
        // enabled, billing). Every mounted picker removes itself.
        window.gm_authFailure = function () {
          failed = true;
          document.querySelectorAll('[data-hs-map]').forEach(function (el) { el.hidden = true; });
        };
        var s = document.createElement('script');
        s.async = true;
        s.src = 'https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(cfg.api_key) +
          '&v=weekly&loading=async&libraries=places,marker&callback=__hsMapsReady';
        s.onerror = function () { loadPromise = null; reject(new Error('maps script')); };
        document.head.appendChild(s);
      });
    }
    return loadPromise;
  }

  function css() {
    if (document.getElementById('hs-map-css')) return;
    var st = document.createElement('style');
    st.id = 'hs-map-css';
    st.textContent =
      '.hs-map-wrap{display:flex;flex-direction:column;gap:8px;margin:0 0 14px;}' +
      '.hs-map-wrap[hidden]{display:none;}' +
      '.hs-map-wrap gmp-place-autocomplete{width:100%;color-scheme:light;}' +
      '.hs-map-canvas{height:220px;border:1px solid var(--line,#e6ddd6);border-radius:12px;overflow:hidden;background:#f4efe9;}' +
      '.hs-map-note{font-size:11.5px;color:var(--text-soft,#7a6f68);margin:0;line-height:1.5;}' +
      '.hs-map-note strong{color:var(--text,#2b2420);font-weight:600;}' +
      '.hs-map-row{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;}' +
      '.hs-map-clear{background:none;border:0;padding:0;font:inherit;font-size:11.5px;color:var(--wine,#7a2e3a);text-decoration:underline;cursor:pointer;}' +
      '.hs-map-err{font-size:11.5px;color:#B4483F;margin:0;}' +
      '@media (max-width:520px){.hs-map-canvas{height:190px;}}';
    document.head.appendChild(st);
  }

  function coord(p, axis) {
    if (!p) return null;
    return typeof p[axis] === 'function' ? p[axis]() : p[axis];
  }

  /* Place API (New) address components -> our address fields. Only what
     Google actually returned is filled; nothing is guessed. */
  function fieldsFrom(place) {
    var by = {};
    (place.addressComponents || []).forEach(function (c) {
      (c.types || []).forEach(function (t) { if (!by[t]) by[t] = c; });
    });
    var long = function (t) { return by[t] ? (by[t].longText || '') : ''; };
    var street = [long('street_number'), long('route')].filter(Boolean).join(' ');
    var line1 = [long('subpremise'), long('premise'), street].filter(Boolean).join(', ');
    if (!line1 && place.displayName && place.displayName !== long('locality')) line1 = place.displayName;
    var area = [long('neighborhood'), long('sublocality_level_2'),
                long('sublocality_level_1') || long('sublocality')].filter(Boolean);
    return {
      line1: line1,
      line2: area.filter(function (v, i) { return area.indexOf(v) === i; }).join(', '),
      city: long('locality') || long('postal_town') || long('administrative_area_level_3') ||
            long('administrative_area_level_2'),
      state: long('administrative_area_level_1'),
      postal_code: long('postal_code'),
      country: long('country')
    };
  }

  /* mount(root, {apiBase, initial, onPlace}) -> Promise<picker|null>
     picker.value() -> {latitude, longitude, place_id, formatted_address} (nulls when unset)
     picker.set(loc)  picker.clear() */
  function mount(root, opts) {
    opts = opts || {};
    if (!root || failed) return Promise.resolve(null);
    return config(opts.apiBase).then(function (cfg) {
      if (!cfg) return null;
      return load(cfg).then(function () {
        return Promise.all([google.maps.importLibrary('maps'), google.maps.importLibrary('places'),
                            cfg.map_id ? google.maps.importLibrary('marker') : null]);
      }).then(function (libs) {
        if (failed) return null;
        return build(root, cfg, libs, opts);
      }).catch(function () { return null; });
    });
  }

  function build(root, cfg, libs, opts) {
    css();
    var maps = libs[0], places = libs[1], markerLib = libs[2];
    var state = { latitude: null, longitude: null, place_id: null, formatted_address: null };

    root.innerHTML =
      '<div class="hs-map-wrap" data-hs-map>' +
        '<div class="hs-map-search"></div>' +
        '<div class="hs-map-canvas" role="region" aria-label="Map — tap to place the delivery pin"></div>' +
        '<div class="hs-map-row"><p class="hs-map-note" data-hs-map-note>Search for your address, or tap the map to drop a pin on your door. ' +
          'The fields below stay editable — they are what goes on the parcel.</p>' +
          '<button type="button" class="hs-map-clear" data-hs-map-clear hidden>Remove pin</button></div>' +
        '<p class="hs-map-err" data-err="location" hidden></p>' +
      '</div>';
    var wrap = root.firstChild;
    var note = wrap.querySelector('[data-hs-map-note]');
    var clearBtn = wrap.querySelector('[data-hs-map-clear]');

    var map = new maps.Map(wrap.querySelector('.hs-map-canvas'), {
      center: INDIA, zoom: 4, mapId: cfg.map_id || undefined,
      streetViewControl: false, mapTypeControl: false, fullscreenControl: false,
      clickableIcons: false, gestureHandling: 'cooperative'
    });
    var marker = null;

    function pinAt(pos, keepPlace) {
      state.latitude = Math.round(coord(pos, 'lat') * 1e7) / 1e7;
      state.longitude = Math.round(coord(pos, 'lng') * 1e7) / 1e7;
      // A pin moved by hand is no longer exactly the searched place.
      if (!keepPlace) state.place_id = null;
      if (!marker) {
        if (markerLib) {
          marker = new markerLib.AdvancedMarkerElement({ map: map, position: pos, gmpDraggable: true });
          marker.addListener('dragend', function () { pinAt(marker.position, false); });
        } else {
          marker = new google.maps.Marker({ map: map, position: pos, draggable: true });
          marker.addListener('dragend', function (e) { pinAt(e.latLng, false); });
        }
      } else if (markerLib) {
        marker.position = pos;
      } else {
        marker.setPosition(pos);
      }
      clearBtn.hidden = false;
      note.innerHTML = '<strong>Pin placed.</strong> Drag it, or tap the map, if it is not on your door.';
    }

    function clear() {
      state = { latitude: null, longitude: null, place_id: null, formatted_address: null };
      if (marker) { if (markerLib) marker.map = null; else marker.setMap(null); marker = null; }
      clearBtn.hidden = true;
      note.textContent = 'Search for your address, or tap the map to drop a pin on your door.';
    }

    map.addListener('click', function (e) { if (e.latLng) pinAt(e.latLng, false); });
    clearBtn.addEventListener('click', clear);

    function usePlace(place) {
      return place.fetchFields({ fields: ['addressComponents', 'formattedAddress', 'location', 'id', 'displayName'] })
        .then(function () {
          if (!place.location) return;
          state.place_id = place.id || null;
          state.formatted_address = place.formattedAddress || null;
          pinAt(place.location, true);
          map.setCenter(place.location);
          map.setZoom(17);
          if (opts.onPlace) opts.onPlace(fieldsFrom(place));
        }).catch(function () {});
    }

    if (places.PlaceAutocompleteElement) {
      var search = new places.PlaceAutocompleteElement({});
      search.setAttribute('aria-label', 'Search for your address');
      // Current event, then the beta-era one: whichever this release fires.
      search.addEventListener('gmp-select', function (e) {
        if (e.placePrediction) usePlace(e.placePrediction.toPlace());
      });
      search.addEventListener('gmp-placeselect', function (e) { if (e.place) usePlace(e.place); });
      wrap.querySelector('.hs-map-search').appendChild(search);
    }

    var picker = {
      value: function () { return Object.assign({}, state); },
      set: function (loc) {
        if (loc && loc.latitude != null && loc.longitude != null) {
          var pos = { lat: Number(loc.latitude), lng: Number(loc.longitude) };
          state.formatted_address = loc.formatted_address || null;
          state.place_id = loc.place_id || null;
          pinAt(pos, true);
          map.setCenter(pos); map.setZoom(16);
        } else { clear(); }
      },
      clear: clear,
      error: function (msg) {
        var el = wrap.querySelector('[data-err="location"]');
        el.textContent = msg || ''; el.hidden = !msg;
      }
    };
    if (opts.initial) picker.set(opts.initial);
    return picker;
  }

  /* A Google Maps link for a saved pin. No key needed; opened in a new tab. */
  function link(loc) {
    if (!loc || loc.latitude == null || loc.longitude == null) return null;
    var url = 'https://www.google.com/maps/search/?api=1&query=' +
      encodeURIComponent(Number(loc.latitude) + ',' + Number(loc.longitude));
    if (loc.place_id) url += '&query_place_id=' + encodeURIComponent(loc.place_id);
    return url;
  }

  window.HSAddressMap = { mount: mount, link: link };
})();
