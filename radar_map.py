"""Camera-centered map using the NWS CONUS reflectivity mosaic."""

import json


def camera_radar_map(camera, reload_count):
    lat, lon = camera.get('latitude'), camera.get('longitude')
    if not (isinstance(lat, (float, int)) and isinstance(lon, (float, int))
            and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    location = json.dumps([lat, lon])
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css">
<style>
  html, body, #map {{ height: 100%; margin: 0; }}
  .camera-pin {{ filter: drop-shadow(0 2px 4px #172033); }}
</style>
<div id="map" aria-label="NWS radar and selected traffic camera"></div>
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const locationPoint = {location};
  const map = L.map('map').setView(locationPoint, 11);
  L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }}).addTo(map);
  const radar = L.tileLayer.wms(
    'https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows', {{
      layers: 'conus:conus_bref_qcd', format: 'image/png', transparent: true,
      opacity: 0.72, attribution: 'NOAA / NWS'
    }}
  ).addTo(map);
  const status = L.control({{position: 'topright'}});
  status.onAdd = () => {{
    const element = L.DomUtil.create('div', 'leaflet-bar leaflet-tooltip');
    element.textContent = 'Loading radar…';
    radar.on('loading', () => {{ element.textContent = 'Loading radar…'; }});
    radar.on('tileerror', () => {{ element.textContent = 'Some radar tiles unavailable'; }});
    radar.on('tileload', () => {{
      if (element.textContent === 'Loading radar…') element.textContent = 'NWS reflectivity';
    }});
    return element;
  }};
  status.addTo(map);
  function refreshRadar() {{
    radar.setParams({{_ts: Date.now() + {int(reload_count)}}});
  }}
  refreshRadar();
  setInterval(refreshRadar, 5 * 60 * 1000);
  const icon = L.divIcon({{
    className: 'camera-pin',
    iconSize: [46, 58],
    iconAnchor: [23, 54],
    html: '<svg width="46" height="58" viewBox="0 0 46 58" aria-hidden="true">' +
      '<path d="M23 54 C19 47 3 35 3 23 A20 20 0 1 1 43 23 C43 35 27 47 23 54Z" fill="#d61a52" stroke="white" stroke-width="3"/>' +
      '<path d="M12 17 H27 V29 H12 Z M27 21 L34 17 V29 L27 25 Z" fill="white"/></svg>'
  }});
  L.marker(locationPoint, {{icon, alt: 'Selected traffic camera', zIndexOffset: 1000}})
    .addTo(map);
  const recenter = L.control({{position: 'topleft'}});
  recenter.onAdd = () => {{
    const button = L.DomUtil.create('button', 'leaflet-bar');
    button.type = 'button';
    button.textContent = 'Center on camera';
    L.DomEvent.disableClickPropagation(button);
    button.onclick = () => map.setView(locationPoint, 11, {{animate: true}});
    return button;
  }};
  recenter.addTo(map);

  const outlook48 = L.layerGroup().addTo(map);
  const outlook7 = L.layerGroup().addTo(map);
  L.control.layers(null, {{'NHC 48-hour locations': outlook48, 'NHC 7-day areas and locations': outlook7}},
                   {{position: 'topright', collapsed: false}}).addTo(map);
  const overview = L.control({{position: 'topleft'}});
  let showOutlook;
  overview.onAdd = () => {{
    showOutlook = L.DomUtil.create('button', 'leaflet-bar');
    showOutlook.type = 'button';
    showOutlook.textContent = 'Show camera and outlook';
    showOutlook.disabled = true;
    L.DomEvent.disableClickPropagation(showOutlook);
    return showOutlook;
  }};
  overview.addTo(map);

  const service = 'https://mapservices.weather.noaa.gov/tropical/rest/services/tropical/NHC_tropical_weather/MapServer';
  async function outlookLayer(id, horizon, group) {{
    const query = new URLSearchParams({{
      where: "UPPER(basin) IN ('AL','ATL','ATLC','ATLANTIC')",
      outFields: 'prob' + horizon + 'day,risk' + horizon + 'day',
      outSR: '4326', f: 'geojson'
    }});
    const response = await fetch(service + '/' + id + '/query?' + query, {{signal: AbortSignal.timeout(12000)}});
    if (!response.ok) throw new Error('Outlook request failed');
    const data = await response.json();
    if (data.type !== 'FeatureCollection' || !Array.isArray(data.features))
      throw new Error('Invalid outlook response');
    function color(feature) {{
      const risk = String(feature.properties['risk' + horizon + 'day']).toLowerCase();
      const probability = parseFloat(feature.properties['prob' + horizon + 'day']);
      if (risk === 'high' || probability > 60) return '#d92131';
      if (risk === 'medium' || probability >= 40) return '#ed7a20';
      return '#e5b700';
    }}
    return L.geoJSON(data, {{
      style: feature => ({{color: color(feature), weight: 2, fillOpacity: 0.20}}),
      pointToLayer: (feature, point) => {{
        const marker = L.circleMarker(point, {{radius: horizon === 2 ? 9 : 5, color: color(feature),
          weight: 3, dashArray: horizon === 2 ? '3 3' : null, fillOpacity: horizon === 2 ? 0 : 0.8}});
        return marker;
      }},
      onEachFeature: (feature, layer) => {{
        const label = document.createElement('span');
        const probability = feature.properties['prob' + horizon + 'day'];
        label.textContent = (horizon === 2 ? '48-hour' : '7-day') + ' formation outlook' +
          (probability == null || probability === '' ? '' : ' · ' + String(probability).replace(/%$/, '') + '%');
        layer.bindTooltip(label);
      }}
    }}).addTo(group);
  }}
  const outlookStatus = L.control({{position: 'bottomleft'}});
  let outlookMessage;
  outlookStatus.onAdd = () => {{
    outlookMessage = L.DomUtil.create('div', 'leaflet-bar leaflet-tooltip');
    outlookMessage.textContent = 'Loading NHC outlook…';
    return outlookMessage;
  }};
  outlookStatus.addTo(map);
  Promise.allSettled([outlookLayer(1, 2, outlook48), outlookLayer(2, 7, outlook7),
                      outlookLayer(3, 7, outlook7)]).then(results => {{
    const bounds = L.latLngBounds([locationPoint]);
    let hasFeatures = false;
    for (const result of results) {{
      if (result.status === 'fulfilled' && result.value.getBounds().isValid()) {{
        bounds.extend(result.value.getBounds());
        hasFeatures = true;
      }}
    }}
    showOutlook.disabled = !hasFeatures;
    showOutlook.onclick = () => {{
      map.addLayer(outlook48);
      map.addLayer(outlook7);
      map.fitBounds(bounds, {{padding: [32, 32], maxZoom: 9}});
    }};
    outlookMessage.textContent = results.some(result => result.status === 'rejected')
      ? 'Some NHC outlook layers unavailable'
      : hasFeatures ? 'NHC formation outlook' : 'No Atlantic disturbances in this outlook';
  }});
</script>
</html>"""
