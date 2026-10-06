from datetime import timezone
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
import httpx
import microsoft_workspace as ms
import workspace_calendar as calendar

@pytest.mark.parametrize('value',['User Name <me@example.com>','a@example.com\nBcc: b@example.com','a..b@example.com','a@-bad.example','not-address','a@example..com'])
def test_recipient_syntax_never_guesses_or_allows_header_injection(value):
    with pytest.raises(HTTPException):ms.addresses(value,required=True)

def test_recipients_deduplicate_and_limit():
    assert len(ms.addresses('a@example.com, A@example.com; b@example.com'))==2
    with pytest.raises(HTTPException):ms.addresses([str(i)+'@example.com' for i in range(51)])

@pytest.mark.parametrize('value',['2026-03-08T02:30','2026-11-01T01:30','bad'])
def test_dst_gaps_and_overlaps_are_not_guessed(value):
    with pytest.raises(HTTPException):ms.timestamp(value,'America/New_York')

def test_explicit_offset_and_timezone_conversion():
    assert ms.timestamp('2026-11-01T01:30:00-04:00','America/New_York').hour==5
    assert ms.timestamp('2026-10-06T10:00','America/New_York').hour==14
    assert ms.timestamp('2026-10-06T10:00','America/New_York').tzinfo==timezone.utc

def test_read_default_does_not_grant_write_and_higher_readworks():
    assert not any(ms.capabilities(calendar.SCOPE)[x] for x in ['mail_send','mail_draft','calendar_write'])
    assert ms.capabilities('Mail.ReadWrite')['mail_read']
    for op in ms.OPS:
        with pytest.raises(HTTPException):ms.require(calendar.SCOPE,op)
        ms.require(calendar.WRITE_SCOPE,op)
    assert 'Mail.Send' not in calendar.SCOPE
    assert 'Mail.Send' in calendar.validated_scopes(calendar.WRITE_SCOPE)

def test_graph_endpoint_guard_and_calendar_ids():
    calls=[]
    def request(method,url,headers,**kw):
        calls.append(headers);return httpx.Response(200,json={},request=httpx.Request(method,url))
    client=SimpleNamespace(request=request)
    for path in ['https://evil.example/me/messages','me/../bad']:
        with pytest.raises(ValueError):ms.request_graph(client,'GET',path,'secret')
    assert not calls
    ms.request_graph(client,'GET','me/calendar','secret');assert 'ImmutableId' not in calls[-1]['Prefer']
    ms.request_graph(client,'GET','me/messages/id','secret');assert 'ImmutableId' in calls[-1]['Prefer']

def test_exact_review_digest_is_sensitive_to_recipient_and_content():
    a={'to':'a@example.com','body':'body'}
    assert ms.digest(a)==ms.digest(dict(reversed(list(a.items()))))
    assert ms.digest(a)!=ms.digest({**a,'to':'b@example.com'})
