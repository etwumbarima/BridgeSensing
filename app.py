"""Florida 511 research dashboard. Run: streamlit run app.py"""
import datetime as dt
import hmac
import io
import json
from pathlib import Path
import secrets
import time
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageDraw

from backend import Manager, radar_url

st.set_page_config(page_title='Florida 511 Research Observatory',page_icon='🚦',layout='wide')
st.markdown('''<style>
.stApp{background:#f3f6f2}h1,h2,h3{color:#173c39}div[data-testid="stMetric"]{background:white;border:1px solid #dbe5df;border-radius:10px;padding:16px}
section[data-testid="stSidebar"]{background:#123c3a}section[data-testid="stSidebar"] *{color:#e6f2e8}section[data-testid="stSidebar"] input{color:#163c38}
section[data-testid="stSidebar"] a,section[data-testid="stSidebar"] a *{color:#123c3a!important}
</style>''',unsafe_allow_html=True)


def check_access():
    try:
        password=st.secrets.get('access_password','')
    except (FileNotFoundError,st.errors.StreamlitSecretNotFoundError):
        password=''
    if password and not st.session_state.get('authorized'):
        st.title('Florida 511 · Research workspace')
        with st.form('sign_in'):
            entered=st.text_input('App password',type='password')
            if st.form_submit_button('Sign in'):
                if hmac.compare_digest(entered,str(password)):
                    st.session_state.authorized=True
                    st.rerun()
                else:
                    st.error('Incorrect app password.')
        st.stop()


check_access()

@st.cache_resource
def services():
    return Manager()


@st.cache_data(ttl=60,max_entries=8)
def downloadable(path,mtime):
    return Path(path).read_bytes()


@st.cache_data(ttl=120,max_entries=16)
def thumbnail(workspace_key,source):
    workspace=services().workspace(workspace_key)
    return workspace.analysis.thumbnail(source,workspace.jobs())


def download(file,label,key):
    if file.is_file():
        if file.stat().st_size>150*1024*1024:
            st.info('This file exceeds the 150 MB browser-download limit. Use shorter capture windows or durable external storage for large videos.')
            return
        mime={'.csv':'text/csv','.json':'application/json','.mp4':'video/mp4','.mkv':'video/x-matroska'}.get(file.suffix,'application/octet-stream')
        st.download_button(label,downloadable(str(file),file.stat().st_mtime_ns),file_name=file.name,mime=mime,key=key,on_click='ignore')


key=st.query_params.get('workspace')
if not key:
    key=secrets.token_hex(16)
    st.query_params['workspace']=key
manager=services()
try:
    workspace=manager.workspace(key)
except ValueError as error:
    st.error(str(error));st.stop()
catalog=workspace.catalog()
cameras=catalog['cameras']
by_id={camera['id']:camera for camera in cameras}
st.session_state.setdefault('selected_ids',[])
st.session_state.selected_ids=[cid for cid in st.session_state.selected_ids if cid in by_id]
st.session_state.setdefault('live_enabled',False)
st.session_state.setdefault('line_config',{'axis':'horizontal','position':.5,'confidence':.3})
if st.session_state.get('focus') not in st.session_state.selected_ids:
    st.session_state.focus=st.session_state.selected_ids[-1] if st.session_state.selected_ids else None
focus=st.session_state.focus
if st.session_state.get('bound_camera')!=focus:
    workspace.preview.stop()
    workspace.stop_live()
    st.session_state.bound_camera=focus
    if st.session_state.live_enabled and focus:
        try: manager.start_live(workspace,focus,st.session_state.line_config)
        except ValueError as error: st.session_state.live_error=str(error)

with st.sidebar:
    st.title('F511')
    st.caption('RESEARCH OBSERVATORY')
    page=st.radio('Workspace',['Cameras','Live conditions','Recordings','Video analysis'],key='page')
    st.divider()
    st.caption('Selected cameras')
    for cid in st.session_state.selected_ids:
        st.write(f"**{by_id[cid]['name']}**")
    st.caption(f"{len(st.session_state.selected_ids)} / 4 selected")
    st.link_button('Florida 511 ↗','https://fl511.com/cctv')
    st.caption('FFmpeg ready' if workspace.recorder.ffmpeg() else 'FFmpeg missing: install packages.txt dependencies')
    st.caption('Download results before a hosted app restarts. Keep this private workspace URL to return to its files.')
    with st.expander('Hosting limits'):
        st.write('Streamlit Community Cloud may sleep, reboot, or run out of CPU/memory. Scheduled captures work only while this app process remains running. Local files are not durable cloud storage. One inference worker and four overlapping recordings are allowed per app instance.')

if page!='Live conditions' and workspace.preview.camera_id:
    workspace.preview.stop()

st.caption('FLORIDA TRAFFIC OBSERVATORY')
st.title({'Cameras':'Your next dataset starts here.','Live conditions':'Live traffic, vehicle counts & weather','Recordings':'Capture schedules & recordings','Video analysis':'Vehicle detection & counting'}[page])


def toggle_camera(cid):
    selected=list(st.session_state.selected_ids)
    if st.session_state['camera_'+cid]:
        if len(selected)>=4 and cid not in selected:
            st.session_state['camera_'+cid]=False
            st.session_state.selection_error='Select up to four cameras.'
            return
        if cid not in selected: selected.append(cid)
        st.session_state.focus=cid
    elif cid in selected:
        selected.remove(cid)
    st.session_state.selected_ids=selected


def summary_table(data,in_frame=False):
    directions=list(next(iter(data.get('crossings',{}).values()),{'up':0,'down':0}))
    rows=[]
    for vehicle in ('car','motorcycle','bus','truck'):
        counts=data.get('crossings',{}).get(vehicle,{})
        row={'Vehicle type':vehicle.title(),**{direction:counts.get(direction,0) for direction in directions},'Crossed':sum(counts.values())}
        if in_frame: row['In frame']=data.get('in_frame',{}).get(vehicle,0)
        rows.append(row)
    st.dataframe(pd.DataFrame(rows),hide_index=True,width='stretch')


@st.fragment(run_every='1s')
def live_frames():
    if workspace.preview.camera_id and not workspace.live_id:
        frame,message=workspace.preview.snapshot()
        if frame: st.image(frame,width='stretch')
        st.caption(message)
    display_id=workspace.live_id or getattr(workspace,'last_live_id',None)
    if display_id:
        try:
            data=workspace.live.state(display_id)
            if data['has_frame'] and (workspace.live_id or not workspace.preview.camera_id):
                st.image((workspace.live.path(display_id)/'frame.jpg').read_bytes(),width='stretch')
            st.caption(data['message'])
            a,b=st.columns(2)
            a.metric('Line crossings',data.get('crossing_total',0))
            b.metric('Vehicles in frame',sum(data.get('in_frame',{}).values()))
            summary_table(data,in_frame=True)
            age=max(0,time.time()-data.get('updated',time.time()))
            st.caption(f"{data.get('processing_fps',0)} processed FPS · {data.get('frames',0)} processed frames · {data.get('skipped_frames',0)} skipped · last update {age:.0f}s ago")
            if data['status'] in ('failed','disconnected','stopped'):
                st.warning('This counting session has ended. Start/reset live counting to resume.')
            file=workspace.live.path(display_id)/'crossings.csv'
            if file.exists(): download(file,'Download live crossing events CSV','live_csv')
        except (OSError,ValueError) as error:
            st.warning(str(error))
    elif not workspace.preview.camera_id:
        st.info('Start the live preview or live counting for this camera.')


@st.fragment(run_every='5s')
def recording_activity():
    jobs=workspace.jobs()
    if not jobs:
        st.info('No capture schedules yet.')
    for job in jobs:
        with st.container(border=True):
            left,right=st.columns([4,1])
            left.write(f"**{job['camera']['name']}** · {job['status'].upper()}")
            left.caption(f"{dt.datetime.fromtimestamp(job['start'],dt.timezone.utc).isoformat()} → {dt.datetime.fromtimestamp(job['end'],dt.timezone.utc).isoformat()} · {job['message']}")
            if job['status'] in ('queued','recording'):
                if right.button('Cancel',key='cancel_'+job['id']):
                    try: workspace.cancel(job['id']);st.rerun(scope='fragment')
                    except ValueError as error: st.error(str(error))
            else:
                for file in job['files']:
                    st.caption(f'{file.name} · {file.stat().st_size/1048576:.1f} MB')
                    if st.button('Prepare download',key='prepare_'+file.name):
                        st.session_state['download_'+file.name]=True
                    if st.session_state.get('download_'+file.name):
                        download(file,'Download recording','file_'+file.name)


@st.fragment(run_every='3s')
def analysis_activity():
    jobs=workspace.analysis.states()
    if not jobs:
        st.info('Start an analysis to see results here.');return
    ids=[job['id'] for job in jobs]
    chosen=st.selectbox('Analysis results',ids,format_func=lambda jid:next(j['source_name']+' · '+j['status'] for j in jobs if j['id']==jid),key='analysis_result')
    job=next(job for job in jobs if job['id']==chosen)
    st.write(job['message'])
    if job.get('total_frames'):
        st.progress(min(1.0,job.get('frames',0)/max(1,job['total_frames'])))
    st.caption(f"{job.get('frames',0)} frames processed · {job.get('elapsed',0)}s elapsed")
    if job['status'] in ('queued','running'):
        if st.button('Cancel analysis'):
            workspace.analysis.cancel(chosen);st.rerun(scope='fragment')
    a,b=st.columns(2)
    a.metric('Line crossings',job.get('crossing_total',0));b.metric('Unique tracks observed',job.get('unique_total',0))
    summary_table(job)
    directory=workspace.analysis.job_path(chosen)
    if job['status']=='completed':
        st.video(str(directory/'annotated.mp4'))
        for name in ('counts.csv','crossings.csv','detections.csv','summary.json','annotated.mp4'):
            download(directory/name,'Download '+name,chosen+'_'+name)
    elif job.get('has_preview'):
        st.image((directory/'latest.jpg').read_bytes(),width='stretch')


if page=='Cameras':
    a,b,c,d=st.columns(4)
    a.metric('Camera views',len(cameras));b.metric('Video enabled',sum(c['available'] for c in cameras));c.metric('Selected',len(st.session_state.selected_ids));d.metric('Capturing',sum(j['status']=='recording' for j in workspace.jobs()))
    st.caption('Catalog synced '+(dt.datetime.fromtimestamp(catalog['updated'],dt.timezone.utc).isoformat() if catalog.get('updated') else 'never')+' · The packaged seed is a snapshot; sync for current availability.')
    if st.button('↻ Sync Florida 511',type='primary'):
        try:
            with st.spinner('Refreshing statewide camera directory…'):
                with workspace.sync_lock: workspace.recorder.sync_catalog()
            st.rerun()
        except Exception as error: st.error(str(error))
    query=st.text_input('Search cameras','',placeholder='Location, road, county or camera ID')
    cols=st.columns(3)
    region=cols[0].selectbox('Region',['All']+sorted({c['region'] for c in cameras if c['region']}))
    county=cols[1].selectbox('County',['All']+sorted({c['county'] for c in cameras if c['county']}))
    roadway=cols[2].selectbox('Roadway',['All']+sorted({c['roadway'] for c in cameras if c['roadway']}))
    video_only=st.checkbox('Video-enabled cameras only',value=True)
    matches=[camera for camera in cameras if (not video_only or camera['available']) and (region=='All' or camera['region']==region) and (county=='All' or camera['county']==county) and (roadway=='All' or camera['roadway']==roadway) and (not query or query.casefold() in ' '.join(str(camera.get(k,'')) for k in ('id','name','roadway','region','county','direction')).casefold())]
    st.caption(f'{len(matches):,} matching cameras. Select up to four, then open Live conditions or Recordings.')
    if st.session_state.pop('selection_error',None): st.warning('Select up to four cameras.')
    pages=max(1,(len(matches)+11)//12)
    page_number=st.number_input('Directory page',min_value=1,max_value=pages,value=1,step=1)
    cards=st.columns(3)
    for index,camera in enumerate(matches[(page_number-1)*12:page_number*12]):
        with cards[index%3].container(border=True):
            widget='camera_'+camera['id']
            st.session_state[widget]=camera['id'] in st.session_state.selected_ids
            st.checkbox(camera['name'],key=widget,disabled=not camera['available'],on_change=toggle_camera,args=(camera['id'],))
            st.caption(f"{camera['roadway']} · {camera['direction']} · {camera['county']} County · ID {camera['id']}")
            st.caption('Video enabled' if camera['available'] else 'Snapshot / unavailable')
            st.link_button('Camera snapshot ↗',camera['snapshot'])

elif page=='Live conditions':
    if not st.session_state.selected_ids:
        st.info('Select a camera in the Cameras directory first.');st.stop()
    def focus_changed():
        st.session_state.focus=st.session_state.live_focus_widget
    focus=st.selectbox('Traffic camera',st.session_state.selected_ids,index=st.session_state.selected_ids.index(st.session_state.focus),format_func=lambda cid:by_id[cid]['name'],key='live_focus_widget',on_change=focus_changed)
    # A selectbox change is seen before widget creation on the next full rerun.
    if st.session_state.bound_camera!=focus:
        st.rerun()
    camera=by_id[focus]
    st.caption(f"{camera['county']} County · {camera['roadway']} · {camera.get('latitude')}, {camera.get('longitude')}")
    left,right=st.columns([1,1.15])
    with left:
        st.subheader(camera['name'])
        buttons=st.columns(2)
        if buttons[0].button('Start / reconnect preview'):
            workspace.stop_live();st.session_state.live_enabled=False
            workspace.preview.start(camera)
        if buttons[1].button('Stop live view'):
            workspace.preview.stop();workspace.stop_live();st.session_state.live_enabled=False
        with st.expander('Live vehicle counting',expanded=False):
            with st.form('live_settings'):
                axis=st.selectbox('Counting line',['horizontal','vertical'],key='live_axis')
                position=st.slider('Line position (%)',5,95,50,key='live_position')
                confidence=st.slider('Detection confidence',.1,.9,.3,.05,key='live_confidence')
                if st.form_submit_button('Start / reset live counting',type='primary'):
                    config={'axis':axis,'position':position/100,'confidence':confidence}
                    try:
                        manager.start_live(workspace,focus,config)
                        st.session_state.line_config=config;st.session_state.live_enabled=True
                    except ValueError as error: st.error(str(error))
        if 'live_error' in st.session_state: st.warning(st.session_state.pop('live_error'))
        live_frames()
        st.caption('Counts are estimates. Changing camera or resetting starts a fresh session. Screen directions are not compass directions. Native Streamlit preview refreshes live frames rather than serving a full-frame-rate video stream.')
    with right:
        st.subheader('Weather radar')
        regions={'Follow selected camera':None,'Florida mosaic':'florida','Tallahassee':'KTLH','Panhandle':'KEVX','Jacksonville':'KJAX','Tampa Bay':'KTBW','Melbourne':'KMLB','Miami':'KAMX','Key West':'KBYX'}
        radar=st.selectbox('Radar view',list(regions))
        target=regions[radar]
        url=radar_url(camera if target is None else None,station=target if target not in (None,'florida') else None)
        if st.button('Reload radar'): st.session_state.radar_reload=st.session_state.get('radar_reload',0)+1
        url+='&reload='+str(st.session_state.get('radar_reload',0)) if '?' in url else '?reload='+str(st.session_state.get('radar_reload',0))
        components.iframe(url,height=670,scrolling=False)
        st.link_button('Open / expand NWS radar ↗',url)
        st.caption('NOAA / National Weather Service. Check the radar scan timestamp: source video and weather have independent publication delays.')

elif page=='Recordings':
    st.info('Schedules run only while this hosted app process is awake. Community Cloud sleep/restarts can interrupt captures; download files before redeploying.')
    st.write('Selected: '+(', '.join(by_id[cid]['name'] for cid in st.session_state.selected_ids) or 'none'))
    timezone=st.selectbox('Capture timezone',['America/New_York','America/Chicago','UTC'])
    zone=ZoneInfo(timezone)
    now=dt.datetime.now(zone)+dt.timedelta(minutes=1)
    with st.form('schedule'):
        first,second=st.columns(2)
        start_date=first.date_input('Start date',now.date())
        start_time=first.time_input('Start time',now.time().replace(tzinfo=None,microsecond=0))
        end=now+dt.timedelta(minutes=30)
        end_date=second.date_input('End date',end.date())
        end_time=second.time_input('End time',end.time().replace(tzinfo=None,microsecond=0))
        if st.form_submit_button('Schedule selected cameras',type='primary'):
            try:
                count=manager.schedule(workspace,st.session_state.selected_ids,dt.datetime.combine(start_date,start_time,zone).timestamp(),dt.datetime.combine(end_date,end_time,zone).timestamp())
                st.success(f'{count} capture(s) scheduled.')
            except ValueError as error: st.error(str(error))
    recording_activity()

elif page=='Video analysis':
    st.caption('YOLO26 + ByteTrack · Cars, motorcycles, buses and trucks · Original quality outputs with CSV exports')
    uploaded=st.file_uploader('Upload a traffic video',type=['mp4','mkv','mov','avi'])
    if uploaded and st.button('Add uploaded video'):
        try: workspace.upload(uploaded.name,uploaded.getvalue());st.success('Video added.')
        except ValueError as error: st.error(str(error))
    sources=workspace.analysis.sources(workspace.jobs())
    if sources:
        ids=[source['id'] for source in sources]
        source=st.selectbox('Video to analyze',ids,format_func=lambda sid:next(s['name'] for s in sources if s['id']==sid))
        with st.form('offline_analysis'):
            axis=st.selectbox('Counting line orientation',['horizontal','vertical'])
            position=st.slider('Line position (%)',5,95,50)
            confidence=st.slider('Confidence',.1,.9,.3,.05)
            duration=st.selectbox('Analyze duration',[0,30,60],format_func=lambda n:'Entire video' if n==0 else f'First {n} seconds')
            if st.form_submit_button('Detect, track & count',type='primary'):
                try:
                    manager.enqueue_analysis(workspace,{'source':source,'axis':axis,'position':position/100,'confidence':confidence,'max_seconds':duration})
                    st.success('Analysis queued.')
                except ValueError as error: st.error(str(error))
        with st.expander('Preview counting line'):
            try:
                image=Image.open(io.BytesIO(thumbnail(key,source))).convert('RGB')
                draw=ImageDraw.Draw(image)
                if axis=='horizontal': draw.line((0,image.height*position/100,image.width,image.height*position/100),fill='yellow',width=3)
                else: draw.line((image.width*position/100,0,image.width*position/100,image.height),fill='yellow',width=3)
                st.image(image,width='stretch')
            except Exception: st.info('Preview unavailable for this video.')
    else:
        st.info('Upload a video or complete a camera recording to begin.')
    analysis_activity()

st.divider()
st.caption('Camera data: Florida 511 / FDOT · Radar: NOAA / NWS · Independent research application. Save this workspace URL; do not share it if its files should remain private.')
