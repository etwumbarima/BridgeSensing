"""Florida weather alerts and camera selection for recording plans."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import re
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st


API = 'https://api.weather.gov'
ZONE = re.compile(r'https://api\.weather\.gov/zones/(?:county|forecast|fire|coastal|offshore)/[A-Z0-9]+')


def nws_json(url):
    request = Request(url, headers={'User-Agent': 'INTELLIVIZ Florida traffic research',
                                    'Accept': 'application/geo+json'})
    with urlopen(request, timeout=8) as response:
        return json.load(response)


@st.cache_data(ttl=60, show_spinner=False)
def florida_alerts():
    return nws_json(f'{API}/alerts/active?area=FL')['features']


def _open_alert(alert_id):
    st.session_state.weather_unread.pop(alert_id, None)
    st.session_state.weather_alert_to_open = alert_id
    st.session_state.page = 'Weather Planning'


@st.fragment(run_every='1m')
def weather_notifications():
    try:
        alerts = florida_alerts()
    except (OSError, KeyError, ValueError):
        st.caption('Weather alert checks temporarily unavailable.')
        return

    active = {alert['id']: alert['properties'] for alert in alerts}
    seen = st.session_state.setdefault('weather_seen', set(active))
    unread = st.session_state.setdefault('weather_unread', {})
    for alert_id in list(unread):
        if alert_id not in active:
            unread.pop(alert_id)
    for alert_id, properties in active.items():
        if alert_id not in seen:
            unread[alert_id] = properties
    seen.update(active)

    with st.expander(f'Weather alerts · {len(unread)} new', expanded=bool(unread)):
        if not unread:
            st.caption('No new alerts since opening INTELLIVIZ.')
        for alert_id, properties in unread.items():
            st.write(f"**{properties['event']}**")
            st.caption(properties.get('areaDesc') or 'Affected area details available')
            if st.button('View affected areas and cameras', key=f'weather_notice_{alert_id}',
                         on_click=_open_alert, args=(alert_id,)):
                st.rerun()


@st.cache_data(ttl=3600, show_spinner=False)
def zone_geometry(url):
    return nws_json(url).get('geometry')


def zone_or_none(url):
    try:
        return zone_geometry(url)
    except (OSError, ValueError):
        return None


@st.cache_data(ttl=300, show_spinner=False)
def alert_geometries(geometry, zone_urls):
    if geometry:
        return [geometry], False
    urls = list(dict.fromkeys(url for url in zone_urls if ZONE.fullmatch(url)))
    if not urls:
        return [], True
    with ThreadPoolExecutor(max_workers=8) as pool:
        geometries = list(pool.map(zone_or_none, urls))
    return [shape for shape in geometries if shape], any(shape is None for shape in geometries)


def match_cameras(cameras, geometries):
    polygons = []
    for geometry in geometries:
        if geometry['type'] not in ('Polygon', 'MultiPolygon'):
            continue
        parts = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
        polygons.extend([np.asarray(ring, dtype=np.float32) for ring in polygon] for polygon in parts if polygon)
    matches = []
    for camera in cameras:
        lat, lon = camera.get('latitude'), camera.get('longitude')
        if not camera['available'] or not isinstance(lat, (float, int)) or not isinstance(lon, (float, int)):
            continue
        point = (float(lon), float(lat))
        if any(cv2.pointPolygonTest(outer, point, False) >= 0
               and all(cv2.pointPolygonTest(hole, point, False) < 0 for hole in holes)
               for outer, *holes in polygons):
            matches.append(camera)
    return matches


def alert_time(value):
    if not value:
        return 'Unavailable'
    return datetime.fromisoformat(value).astimezone(ZoneInfo('America/New_York')).strftime('%b %d, %I:%M %p %Z')


@st.fragment(run_every='1s')
def weather_camera_frames(preview):
    frame, message = preview.snapshot()
    if frame:
        st.image(frame, width='stretch')
    st.caption(message)


def render_weather_planning(cameras, preview):
    st.caption('Florida watches, warnings and advisories from the National Weather Service.')
    if st.button('Refresh alerts'):
        florida_alerts.clear()
    try:
        alerts = florida_alerts()
    except (OSError, KeyError, ValueError):
        st.warning('Florida weather alerts are temporarily unavailable.')
        return
    if not alerts:
        st.info('NWS reports no active Florida alerts.')
        return
    by_id = {alert['id']: alert for alert in alerts}
    requested = st.session_state.pop('weather_alert_to_open', None)
    if requested in by_id:
        st.session_state.weather_active_alert = requested
    elif requested:
        st.info('That alert has ended. Choose an active alert below.')
    selected = st.selectbox('Active Florida alert', list(by_id), format_func=lambda key:
                            by_id[key]['properties'].get('headline') or by_id[key]['properties']['event'],
                            key='weather_active_alert')
    alert = by_id[selected]
    properties = alert['properties']
    st.warning(properties.get('headline') or properties['event'])
    st.write(properties.get('areaDesc') or 'Area description unavailable')
    st.caption(f"Starts {alert_time(properties.get('onset') or properties.get('effective'))} · "
               f"Ends {alert_time(properties.get('ends') or properties.get('expires'))}")
    with st.expander('NWS alert details'):
        st.text(properties.get('description') or 'Details unavailable')
        if properties.get('instruction'):
            st.text(properties['instruction'])
    with st.spinner('Matching cameras to the NWS alert area…'):
        shapes, incomplete = alert_geometries(alert.get('geometry'), properties.get('affectedZones', []))
        matches = match_cameras(cameras, shapes)
    if incomplete:
        st.info('Some alert boundaries are unavailable. Camera matches may be incomplete.')
    if shapes:
        layers = [pdk.Layer('GeoJsonLayer', {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'geometry': shape, 'properties': {}} for shape in shapes]},
            id='alert-area', get_fill_color=[224, 88, 35, 70], get_line_color=[185, 57, 23],
            line_width_min_pixels=2)]
        if matches:
            st.caption('Click a blue camera marker to open its preview controls.')
            layers.append(pdk.Layer('ScatterplotLayer', pd.DataFrame(matches)[['id', 'latitude', 'longitude', 'name']],
                                    id='weather-cameras',
                                    get_position='[longitude, latitude]', get_fill_color=[16, 87, 120],
                                    radius_min_pixels=7, pickable=True))
        deck = pdk.Deck(layers=layers, map_provider='carto', map_style='light',
                        initial_view_state=pdk.ViewState(latitude=27.9, longitude=-82.6, zoom=5.2),
                        tooltip={'text': '{name}'})
        if matches:
            event = st.pydeck_chart(deck, key=f'weather_map_{selected}', on_select='rerun',
                                   selection_mode='single-object')
            selected_objects = event.selection.objects.get('weather-cameras', [])
            camera_id = selected_objects[0].get('id') if selected_objects else None
            camera = next((item for item in matches if item['id'] == camera_id), None)
            if preview.camera_id and preview.camera_id != camera_id:
                preview.stop()
            if camera:
                st.subheader(camera['name'])
                if st.button('Start live preview', key=f'weather_start_{camera_id}'):
                    preview.start(camera)
                if preview.camera_id == camera_id:
                    if st.button('Stop preview', key=f'weather_stop_{camera_id}'):
                        preview.stop()
                    else:
                        weather_camera_frames(preview)
                st.link_button('Open live camera on Florida 511 ↗',
                               f'https://fl511.com/map/Cctv/{quote(str(camera_id), safe="")}')
        else:
            st.pydeck_chart(deck)
    st.subheader(f'{len(matches):,} video-enabled cameras in the mapped area')
    if matches:
        names = {camera['id']: f"{camera['name']} · {camera['county']}" for camera in matches}
        remaining = 4 - len(st.session_state.selected_ids)
        options = [key for key in names if key not in st.session_state.selected_ids]
        if remaining and options:
            additions = st.multiselect('Add cameras to your recording selection', options, max_selections=remaining,
                                       format_func=names.get, key=f'weather_cameras_{selected}')
            if additions and st.button('Add selected cameras', on_click=_add_cameras, args=(additions,)):
                st.rerun()
        st.button('Open Recordings', on_click=_open_recordings)


def _add_cameras(camera_ids):
    st.session_state.selected_ids = list(dict.fromkeys([*st.session_state.selected_ids, *camera_ids]))[:4]


def _open_recordings():
    st.session_state.page = 'Recordings'
