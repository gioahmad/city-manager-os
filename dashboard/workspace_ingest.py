"""Bounded document extraction and a separate worker for private intake and Microsoft sync."""
import csv
import io
import json
import multiprocessing
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

MAX_TEXT=200000
SUPPORTED={'.pdf','.txt','.md','.csv','.tsv','.json','.docx','.xlsx','.log'}


def zip_check(content):
    archive=zipfile.ZipFile(io.BytesIO(content))
    files=archive.infolist()
    if len(files)>10000 or sum(f.file_size for f in files)>100*1024*1024:
        raise ValueError('Expanded document exceeds the 100 MB processing limit.')
    return archive


def table_profile(rows):
    header=next(rows,[])
    if len(header)>200:raise ValueError('Dataset has more than 200 columns. Split it into smaller datasets.')
    columns=[str(v if v is not None else '')[:200] or 'Column '+str(i+1) for i,v in enumerate(header)]
    sample=[];count=0;missing=[0]*len(columns);numeric=[[] for _ in columns]
    text=[ '\t'.join(columns) ];size=len(text[0]);limited=False
    for row in rows:
        count+=1
        if count>100000:raise ValueError('Dataset exceeds 100,000 rows. Split it into smaller files.')
        values=[str(row[i] if i<len(row) and row[i] is not None else '')[:2000] for i in range(len(columns))]
        for i,v in enumerate(values):
            if not v.strip():missing[i]+=1
            if len(numeric[i])<10000:
                try:
                    value=float(v)
                    if value==value and abs(value)<1e100:numeric[i].append(value)
                except (ValueError,OverflowError):pass
        if len(sample)<20:sample.append(values)
        line='\t'.join(values)
        if size+len(line)+1<MAX_TEXT:text.append(line);size+=len(line)+1
        else:limited=True
    stats=[]
    for i,name in enumerate(columns):
        values=numeric[i]
        stats.append({'name':name,'missing':missing[i], 'numeric_sample_count':len(values),
                      **({'min':min(values),'max':max(values),'mean':sum(values)/len(values)} if values else {})})
    return '\n'.join(text),{'type':'dataset','rows':count,'columns':columns,'sample':sample,'column_stats':stats,
                            'search_text_limited':limited,'numeric_stats_limit':10000}


def extract(filename,content):
    extension=Path(filename).suffix.lower()
    if extension not in SUPPORTED:raise ValueError('Unsupported file type. Use PDF, DOCX, XLSX, CSV, TSV, JSON, Markdown, or text.')
    profile={'type':'document','format':extension.lstrip('.')}
    state='READY'
    if extension=='.pdf':
        from pypdf import PdfReader
        reader=PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(''):raise ValueError('Unlock this PDF before uploading.')
        if len(reader.pages)>500:raise ValueError('PDF exceeds 500 pages. Split it into smaller files.')
        parts=[];size=0
        for page in reader.pages:
            part=page.extract_text() or '';parts.append(part[:MAX_TEXT-size]);size+=len(parts[-1])
            if size>=MAX_TEXT:break
        body='\n\n'.join(parts)[:MAX_TEXT];profile['pages']=len(reader.pages)
        if not body.strip():state='NEEDS_OCR';profile['note']='Image-only PDF. The original is retained; OCR is not installed.'
    elif extension=='.docx':
        with zip_check(content) as archive:
            root=ElementTree.fromstring(archive.read('word/document.xml'))
            body='\n'.join(''.join(p.itertext()) for p in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'))[:MAX_TEXT]
    elif extension=='.xlsx':
        from openpyxl import load_workbook
        with zip_check(content):pass
        workbook=load_workbook(io.BytesIO(content),read_only=True,data_only=True)
        try:
            sheets=[];parts=[]
            for sheet in workbook.worksheets[:20]:
                text,details=table_profile(iter(sheet.values));sheets.append({'name':sheet.title,**details});parts.append(sheet.title+'\n'+text)
            body='\n\n'.join(parts)[:MAX_TEXT];profile={'type':'workbook','sheets':sheets,'sheet_limit':20}
        finally:workbook.close()
    else:
        try:body=content.decode('utf-8-sig')
        except UnicodeDecodeError:body=content.decode('cp1252')
        if '\x00' in body:raise ValueError('This is a binary file. Convert it to one of the supported text formats.')
        if extension in {'.csv','.tsv'}:
            body,profile=table_profile(iter(csv.reader(io.StringIO(body),delimiter='\t' if extension=='.tsv' else ',')))
        elif extension=='.json':
            data=json.loads(body)
            if isinstance(data,list) and data and isinstance(data[0],dict):
                if len(data)>100000:raise ValueError('Dataset exceeds 100,000 rows.')
                columns=list(dict.fromkeys(k for row in data if isinstance(row,dict) for k in row))
                def rows():
                    yield columns
                    for row in data:
                        if not isinstance(row,dict):raise ValueError('JSON dataset rows must all be objects.')
                        yield [json.dumps(row.get(k),ensure_ascii=False) if isinstance(row.get(k),(dict,list)) else row.get(k) for k in columns]
                body,profile=table_profile(rows())
            else:body=json.dumps(data,ensure_ascii=False,indent=2)[:MAX_TEXT];profile['type']='json'
        else:body=body[:MAX_TEXT]
    body=body.replace('\x00','')[:MAX_TEXT]
    profile['characters']=len(body)
    profile['emails']=sorted(set(re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',body)))[:50]
    profile['dates']=sorted(set(re.findall(r'\b\d{4}-\d{2}-\d{2}\b',body)))[:50]
    return {'status':state,'text':body,'profile':profile}


def _child(pipe,filename,content):
    # Parsing happens outside the web process and has a hard memory/time limit.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
        resource.setrlimit(resource.RLIMIT_CPU,(45,45))
        pipe.send(extract(filename,content))
    except Exception:
        pipe.send({'status':'FAILED','text':'','profile':{},'error':'Could not process this file. Check its format, size, and encryption, then retry.'})
    finally:pipe.close()


def bounded_extract(filename,content):
    context=multiprocessing.get_context('spawn');receive,send=context.Pipe(duplex=False)
    process=context.Process(target=_child,args=(send,filename,content));process.start();send.close()
    try:
        if receive.poll(55):
            try:return receive.recv()
            except EOFError:pass
        return {'status':'FAILED','text':'','profile':{},'error':'Processing reached its resource limit. Split the file into smaller parts and retry.'}
    finally:
        if process.is_alive():process.terminate()
        process.join(timeout=3)
        if process.is_alive():process.kill();process.join()
        receive.close()


def process_one():
    from app import db_conn
    with db_conn() as c:
        c.execute("""UPDATE workspace_documents SET status='QUEUED',started_at=NULL
            WHERE status='PROCESSING' AND started_at<now()-interval '5 minutes'""")
        row=c.execute("""SELECT id,filename,content FROM workspace_documents WHERE status='QUEUED'
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not row:return False
        c.execute("UPDATE workspace_documents SET status='PROCESSING',started_at=now(),attempts=attempts+1,updated_at=now() WHERE id=%s",(row['id'],))
    result=bounded_extract(row['filename'],bytes(row['content']))
    with db_conn() as c:
        c.execute('''UPDATE workspace_documents SET status=%s,extracted_text=%s,profile=%s::jsonb,error=%s,updated_at=now()
            WHERE id=%s AND status='PROCESSING' ''',
            (result['status'],result['text'],json.dumps(result['profile']),result.get('error'),row['id']))
    return True


def run():
    from app import db_conn,query_all
    from workspace_calendar import settings,sync
    attempted={};next_sync=0
    while True:
        try:
            with db_conn() as c:c.execute("SELECT 1")
            # A heartbeat makes a disconnected/stuck worker visible to Docker and the installer.
            Path('/tmp/workspace-intake-heartbeat').write_text(str(time.time()))
            processed=process_one()
            if time.monotonic()>=next_sync and settings()['ready']:
                next_sync=time.monotonic()+60
                rows=query_all('SELECT owner_username FROM workspace_calendar_connections ORDER BY last_sync_at NULLS FIRST LIMIT 25')
                for row in rows:
                    owner=row['owner_username']
                    if time.monotonic()-attempted.get(owner,-1000)<900:continue
                    attempted[owner]=time.monotonic()
                    try:sync(owner)
                    except Exception:print('Microsoft refresh needs attention; previous owner snapshot retained.',flush=True)
                    Path('/tmp/workspace-intake-heartbeat').write_text(str(time.time()))
            if not processed:time.sleep(3)
        except Exception:
            print('Private intake worker is waiting for the database.',flush=True);time.sleep(10)


if __name__=='__main__':run()
