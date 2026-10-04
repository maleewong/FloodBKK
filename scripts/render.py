"""Render the offline pages from data/*.json (stdlib only).
bkk_drainage.html  <- template.html + dashboard_data.json (+ canal layer from network.json)
bkk_model.html     <- template.html (head/CSS) + model_body.html + model_data.json + solver.js + model_core.js"""
import os, json, re
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.join(HERE, '..'); DATA = os.path.join(ROOT, 'data')
rd = lambda p: open(p, encoding='utf-8').read()
safe = lambda s: s.replace('</', '<\\/')
DOC = '<!doctype html>\n<html lang="th"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">\n'
STANDALONE = True   # False -> no doctype (for publishing as a hosted page that adds its own skeleton)


def canal_layer():
    """compact canal geometry for the overview map: per edge [nameIdx, class, stationCode, coords...]"""
    net = json.load(open(os.path.join(DATA, 'network.json'), encoding='utf-8'))
    edges = [[e['n'], e['c'], e.get('st') or '', [c for xy in e['g'] for c in xy]] for e in net['edges']]
    pumps = [dict(name=p['name'], cap=p['cap'], src=p['src'], lon=net['nodes'][p['node']]['lon'], lat=net['nodes'][p['node']]['lat'])
             for p in net['pumps'] if not p['internal']]
    return dict(names=net['names'], edges=edges, cp=net['chao_phraya'], pumps=pumps)


def gates():
    """water gates with telemetry: [code, name, lat, lon, opening_m, level_in, level_out, warn, crit, status, canal, time]"""
    f = os.path.join(DATA, 'snapshot.json')
    if not os.path.exists(f): return []
    out = []
    for w in json.load(open(f, encoding='utf-8'))['W']:
        if not w[7]: continue                      # stations that report gate openings
        op = max([x for x in (w[8], w[9], w[10]) if isinstance(x, (int, float))] or [0])
        out.append([w[0], (w[1] or '').strip(), w[4], w[5], op, w[13], w[14], w[18], w[19], w[12], w[24], w[11]])
    return out


def main():
    ub = rd(os.path.join(HERE, 'update_button.html'))
    tpl = rd(os.path.join(HERE, 'template.html')).replace('__UPDATE_BUTTON__', ub)
    d = json.load(open(os.path.join(DATA, 'dashboard_data.json'), encoding='utf-8'))
    if os.path.exists(os.path.join(DATA, 'network.json')):
        d['canals'] = canal_layer()
    if os.path.exists(os.path.join(DATA, 'road_segments.json')):      # road under each flood sensor (build_roads.py)
        d['road_seg'] = json.load(open(os.path.join(DATA, 'road_segments.json'), encoding='utf-8'))
    open(os.path.join(ROOT, 'bkk_drainage.html'), 'w', encoding='utf-8').write((DOC if STANDALONE else '') +
        tpl.replace('__DATA__', safe(json.dumps(d, ensure_ascii=False, separators=(',', ':')))))

    if os.path.exists(os.path.join(DATA, 'model_data.json')):
        head = tpl[:tpl.index('</style>') + len('</style>')]
        head = re.sub(r'<title>.*?</title>', '<title>โมเดลระบายน้ำ กทม.</title>', head)
        body = rd(os.path.join(HERE, 'model_body.html')).replace('__UPDATE_BUTTON__', ub)
        js = lambda f: re.sub(r'if\(typeof module.*', '', rd(os.path.join(HERE, f)))
        page = head + body.replace('__SOLVER__', js('solver.js')).replace('__CORE__', js('model_core.js')).replace('__GATES_JS__', rd(os.path.join(HERE, 'gates.js')))
        md0 = json.load(open(os.path.join(DATA, 'model_data.json'), encoding='utf-8'))
        if os.path.exists(os.path.join(DATA, 'outer_paths.json')):
            md0['outer'] = json.load(open(os.path.join(DATA, 'outer_paths.json'), encoding='utf-8'))
        md0['gates'] = gates()
        page = page.replace('__MODEL__', safe(json.dumps(md0, ensure_ascii=False, separators=(',', ':'))))
        open(os.path.join(ROOT, 'bkk_model.html'), 'w', encoding='utf-8').write((DOC if STANDALONE else '') + page)
    if os.path.exists(os.path.join(DATA, 'forecast.json')) and os.path.exists(os.path.join(DATA, 'model_data.json')):
        md = json.load(open(os.path.join(DATA, 'model_data.json'), encoding='utf-8'))
        fd = dict(fetched=md['fetched'], geo=md['geo'], st=md['st'], road_now=md.get('road', []),
                  road=json.load(open(os.path.join(DATA, 'dashboard_data.json'), encoding='utf-8')).get('road', []) if os.path.exists(os.path.join(DATA, 'dashboard_data.json')) else [],
                  road_seg=json.load(open(os.path.join(DATA, 'road_segments.json'), encoding='utf-8')) if os.path.exists(os.path.join(DATA, 'road_segments.json')) else None,
                  net=dict(names=md['net']['names'], nodes=[x[:2] for x in md['net']['nodes']], edges=md['net']['edges'],
                           pumps=[dict(id=p['id'], name=p['name'], node=p['node'], cap=p['cap']) for p in md['net']['pumps'] if not p['internal']],
                           cp=md['net']['cp']),
                  outer=json.load(open(os.path.join(DATA, 'outer_paths.json'), encoding='utf-8')) if os.path.exists(os.path.join(DATA, 'outer_paths.json')) else None,
                  F=json.load(open(os.path.join(DATA, 'forecast.json'), encoding='utf-8')),
                  FL=json.load(open(os.path.join(DATA, 'flood.json'), encoding='utf-8')) if os.path.exists(os.path.join(DATA, 'flood.json')) else None)
        head = tpl[:tpl.index('</style>') + len('</style>')]
        head = re.sub(r'<title>.*?</title>', '<title>คาดการณ์ฝนและการพร่องน้ำ กทม.</title>', head)
        body = rd(os.path.join(HERE, 'forecast_body.html')).replace('__UPDATE_BUTTON__', ub)
        body = body.replace('__FORECAST__', safe(json.dumps(fd, ensure_ascii=False, separators=(',', ':'))))
        open(os.path.join(ROOT, 'bkk_forecast.html'), 'w', encoding='utf-8').write((DOC if STANDALONE else '') + head + body)
    print('rendered', [f for f in ('bkk_drainage.html', 'bkk_model.html', 'bkk_forecast.html') if os.path.exists(os.path.join(ROOT, f))])


if __name__ == '__main__':
    main()
