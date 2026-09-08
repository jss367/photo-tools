from playwright.sync_api import expect


def test_species_groups_preserve_paths_and_chart_drill_down(live_server, page):
    db = live_server['db']
    photos = live_server['data']['photos']
    taxon = db.conn.execute(
        "INSERT INTO taxa(name, common_name, rank, inat_id) VALUES ('Testus bird', 'Test bird', 'species', 123)"
    ).lastrowid
    parent = db.add_keyword('Imported birds')
    root = db.add_keyword('Test bird', is_species=True)
    leaf = db.add_keyword('Test Bird', parent_id=parent, is_species=True)
    db.conn.execute('UPDATE keywords SET taxon_id = ? WHERE id IN (?, ?)', (taxon, root, leaf))
    db.tag_photo(photos[0], root)
    db.tag_photo(photos[0], leaf)
    db.tag_photo(photos[1], leaf)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_server['url'] + '/keywords')
    page.locator('#kwSearch').fill('Test bird')
    expect(page.locator('#kwBody tr')).to_have_count(1)
    row = page.locator('#kwBody tr')
    expect(row.locator('td').nth(5)).to_have_text('2')
    row.locator('summary').click()
    expect(row).to_contain_text('Imported birds → Test Bird')
    row.get_by_role('button', name='Edit individual records').click()
    expect(page.locator('#kwBody tr')).to_have_count(2)
    page.goto(live_server['url'] + '/dashboard')
    bar = page.locator('#speciesChart .species-bar', has_text='Test bird')
    expect(bar).to_have_count(1)
    expect(bar.locator('.bar-value')).to_have_text('2')
    bar.click()
    expect(page.locator('.grid-card')).to_have_count(2)
    assert errors == []


def test_location_preview_combines_only_chosen_match(live_server, page):
    db = live_server['db']
    photos = live_server['data']['photos']
    source = db.add_keyword('Lake Hodges', kw_type='general')
    target = db.upsert_place_chain({
        'place_id': 'test-place', 'name': 'Lake Hodges', 'lat': 33, 'lng': -117,
        'address_components': [{'name': 'San Diego', 'types': ['locality']}],
    })
    db.tag_photo(photos[0], source)
    db.tag_photo(photos[1], target)
    page.goto(live_server['url'] + '/keywords')
    page.locator('#kwSearch').fill('Lake Hodges')
    expect(page.locator('#kwBody tr')).to_have_count(2)
    page.get_by_role('button', name='Review matching locations').click()
    preview = page.locator('#kwLocationMatches')
    expect(preview).to_contain_text('2 distinct photos after combining')
    expect(preview).to_contain_text('San Diego → Lake Hodges')
    preview.get_by_role('button', name='Use this place').click()
    expect(page.locator('#kwBody tr')).to_have_count(1)
    expect(preview).to_have_text('No matching location keywords to review.')
    expect(page.locator('#kwBody tr .kw-linked-badge')).to_be_visible()
    assert db.get_assigned_photo_location(photos[0])['place_id'] == 'test-place'
    assert db.add_keyword('Lake Hodges') == target
