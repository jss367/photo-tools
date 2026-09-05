"""Every browser transport participates in persistent edit history."""

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize('transport', ['fetch', 'request', 'json'])
def test_pending_browser_writes_guard_history_and_refresh_after_save(live_server, page, transport):
    photo_id = live_server['data']['photos'][0]
    page.goto(live_server['url'] + '/browse')
    page.evaluate('''async photoId => {
      await fetch('/api/photos/' + photoId + '/flag', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({flag: 'flagged'})
      });
    }''', photo_id)
    expect(page.locator('#historyUndoBtn')).to_be_enabled()
    held = []
    page.route(f'**/api/photos/{photo_id}/flag', lambda route: held.append(route))
    page.evaluate('''({photoId, transport}) => {
      const url = '/api/photos/' + photoId + '/flag';
      const options = {method: 'POST', headers: {'Content-Type': 'application/json'},
                       body: JSON.stringify({flag: 'rejected'})};
      const write = transport === 'request' ? fetch(new Request(location.origin + url, options))
                  : transport === 'json' ? Vireo.api.json(url, options) : fetch(url, options);
      window.historyTestWrite = write.catch(error => ({error: error.message}));
    }''', {'photoId': photo_id, 'transport': transport})
    expect(page.locator('#historyUndoBtn')).to_be_disabled()
    page.evaluate('doUndo()')
    assert len(held) == 1
    held.pop().continue_()
    page.evaluate('window.historyTestWrite')
    expect(page.locator('#historyUndoBtn')).to_be_enabled()
    assert live_server['db'].get_photo(photo_id)['flag'] == 'rejected'
    page.locator('#historyUndoBtn').click()
    expect(page.locator('#historyRedoBtn')).to_be_enabled()
    assert live_server['db'].get_photo(photo_id)['flag'] == 'flagged'


def test_failed_native_write_releases_history_guard(live_server, page):
    photo_id = live_server['data']['photos'][0]
    page.goto(live_server['url'] + '/browse')
    page.evaluate('''async photoId => {
      await fetch('/api/photos/' + photoId + '/flag', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({flag: 'rejected'})
      });
    }''', photo_id)
    expect(page.locator('#historyUndoBtn')).to_be_enabled()
    page.route(f'**/api/photos/{photo_id}/flag', lambda route: route.abort())
    page.evaluate('''async photoId => {
      try {
        await fetch('/api/photos/' + photoId + '/flag', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({flag: 'flagged'})
        });
      } catch (error) {}
    }''', photo_id)
    expect(page.locator('#historyUndoBtn')).to_be_enabled()
    page.locator('#historyUndoBtn').click()
    expect(page.locator('#historyRedoBtn')).to_be_enabled()
    assert live_server['db'].get_photo(photo_id)['flag'] == 'none'


def test_first_lightbox_adjustment_enables_undo(live_server, page):
    page.goto(live_server['url'] + '/browse')
    expect(page.locator('#historyUndoBtn')).to_be_disabled()
    page.locator('.grid-card').first.dblclick()
    page.locator('#lightboxAdjustBtn').click()
    exposure = page.locator('#lbAdjExposure')
    expect(exposure).to_be_enabled()
    exposure.fill('1')
    exposure.dispatch_event('input')
    expect(page.locator('#historyUndoBtn')).to_be_enabled()
    page.evaluate('doUndo()')
    expect(exposure).to_have_value('0')
    expect(page.locator('#historyRedoBtn')).to_be_enabled()
    page.evaluate('doRedo()')
    expect(exposure).to_have_value('1')
