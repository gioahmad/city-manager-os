import io
import json
import zipfile

import pytest
from workspace_ingest import bounded_extract,extract,table_profile


def test_csv_quotes_empty_values_and_statistics():
    result=extract('budget.csv',b'name,total,notes\n"Rivera, Maria",12,"site, visit"\nLucia,18,\n')
    assert result['status']=='READY'
    profile=result['profile']
    assert profile['rows']==2 and profile['columns']==['name','total','notes']
    assert profile['sample'][0]==['Rivera, Maria','12','site, visit']
    assert profile['column_stats'][1]['mean']==15 and profile['column_stats'][2]['missing']==1


def test_json_table_and_extracted_email_dates():
    result=extract('records.json',json.dumps([{'name':'Maria','email':'maria@example.com','date':'2026-10-03','amount':5},
                                            {'name':'Lucia','amount':7,'address':'Park Avenue'}]).encode())
    assert result['profile']['rows']==2
    assert result['profile']['emails']==['maria@example.com'] and result['profile']['dates']==['2026-10-03']
    assert 'Park Avenue' in result['text'] and 'address' in result['profile']['columns']


def test_docx_text_and_xlsx_multiple_sheets():
    content=io.BytesIO()
    with zipfile.ZipFile(content,'w') as z:
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Waterfront access</w:t></w:r></w:p></w:body></w:document>')
    assert extract('memo.docx',content.getvalue())['text']=='Waterfront access'
    from openpyxl import Workbook
    workbook=Workbook();workbook.active.title='Budget';workbook.active.append(['item','amount']);workbook.active.append(['Inspection',30])
    sheet=workbook.create_sheet('People');sheet.append(['name','phone']);sheet.append(['Maria','+12015550101'])
    content=io.BytesIO();workbook.save(content)
    result=extract('operations.xlsx',content.getvalue())
    assert [s['name'] for s in result['profile']['sheets']]==['Budget','People']
    assert result['profile']['sheets'][0]['column_stats'][1]['max']==30
    assert 'Maria' in result['text']


def test_scanned_and_encrypted_pdf_have_honest_outcomes():
    from pypdf import PdfWriter
    writer=PdfWriter();writer.add_blank_page(width=100,height=100);stream=io.BytesIO();writer.write(stream)
    result=extract('scan.pdf',stream.getvalue())
    assert result['status']=='NEEDS_OCR' and not result['text'] and result['profile']['pages']==1
    writer.encrypt('secret');stream=io.BytesIO();writer.write(stream)
    with pytest.raises(ValueError,match='Unlock'):extract('locked.pdf',stream.getvalue())


def test_resource_limits_and_binary_files_fail():
    with pytest.raises(ValueError,match='200 columns'):table_profile(iter([list(range(201))]))
    with pytest.raises(ValueError,match='binary'):extract('binary.txt',b'a\x00b')
    with pytest.raises(ValueError,match='Unsupported'):extract('file.exe',b'example')
    content=io.BytesIO()
    with zipfile.ZipFile(content,'w',compression=zipfile.ZIP_DEFLATED) as z:z.writestr('oversized',b'0'*(101*1024*1024))
    with pytest.raises(ValueError,match='100 MB'):extract('archive.docx',content.getvalue())


def test_separate_process_extracts_and_handles_invalid_file():
    assert bounded_extract('memo.txt',b'Park Avenue')['text']=='Park Avenue'
    result=bounded_extract('bad.pdf',b'not a PDF')
    assert result['status']=='FAILED' and result['error'] and 'not a PDF' not in result['error']
