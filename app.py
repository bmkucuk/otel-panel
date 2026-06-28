#!/usr/bin/env python3
"""Otel Panel — Yönetim Merkezi"""

import os, json, time, base64, secrets, sqlite3
from datetime import datetime, date
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import requests as req

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'panel2026secret')

# ── Sabitler ──────────────────────────────────────────────────────────────────
PANEL_PASSWORD   = os.environ.get('PANEL_PASSWORD', 'admin123')
GITHUB_TOKEN     = os.environ.get('GITHUB_TOKEN', '')
RENDER_TOKEN     = os.environ.get('RENDER_TOKEN', '')
RENDER_OWNER     = os.environ.get('RENDER_OWNER', 'tea-d86rqqdckfvc73bi7ae0')
GITHUB_USER      = os.environ.get('GITHUB_USER', 'bmkucuk')
TEMPLATE_REPO    = f'{GITHUB_USER}/otel-template'
GMAIL_USER       = os.environ.get('GMAIL_USER', '')
GMAIL_PASS       = os.environ.get('GMAIL_PASS', '')

GITHUB_HEADERS = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
    'Content-Type': 'application/json',
}
RENDER_HEADERS = {
    'Authorization': f'Bearer {RENDER_TOKEN}',
    'Content-Type': 'application/json',
}

DB_PATH = '/data/panel.db' if os.path.isdir('/data') else 'panel.db'

# ── DB ────────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS musteriler (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            otel_ad          TEXT NOT NULL,
            kisa_ad          TEXT NOT NULL,
            repo_adi         TEXT UNIQUE NOT NULL,
            repo_url         TEXT,
            site_url         TEXT,
            sadmin_url       TEXT,
            superadmin_key   TEXT,
            render_service_id TEXT,
            tur              TEXT DEFAULT 'demo',  -- 'demo' veya 'aktif'
            durum            TEXT DEFAULT 'hazirlaniyor', -- 'hazirlaniyor','aktif','askida','demo_bitti'
            oda_sayi         INTEGER DEFAULT 20,
            foy_baslangic    INTEGER DEFAULT 1,
            adisyon_baslangic INTEGER DEFAULT 1,
            sehir            TEXT,
            gmail_user       TEXT,
            gmail_pass       TEXT,
            yedek_mail       TEXT,
            olusturma_tarihi TEXT DEFAULT (datetime('now')),
            notlar           TEXT
        );
        CREATE TABLE IF NOT EXISTS islem_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            tarih   TEXT DEFAULT (datetime('now')),
            musteri_id INTEGER,
            islem   TEXT,
            sonuc   TEXT,
            detay   TEXT
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── Auth ──────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('giris'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login():
    hata = None
    if request.method == 'POST':
        if request.form.get('sifre') == PANEL_PASSWORD:
            session['giris'] = True
            return redirect(url_for('dashboard'))
        hata = 'Şifre yanlış'
    return render_template('login.html', hata=hata)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Ana Sayfa ─────────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def dashboard():
    conn = get_db()
    demolar   = [dict(r) for r in conn.execute("SELECT * FROM musteriler WHERE tur='demo' ORDER BY olusturma_tarihi DESC").fetchall()]
    aktifler  = [dict(r) for r in conn.execute("SELECT * FROM musteriler WHERE tur='aktif' ORDER BY olusturma_tarihi DESC").fetchall()]
    conn.close()
    return render_template('dashboard.html', demolar=demolar, aktifler=aktifler)

# ── API: Yeni Oluştur ─────────────────────────────────────────────────────────
@app.route('/api/olustur', methods=['POST'])
@login_required
def api_olustur():
    d = request.get_json()
    tur = d.get('tur', 'demo')  # 'demo' veya 'aktif'

    # Repo adı
    otel_ad = d['otel_ad'].strip()
    repo_adi = (d.get('repo_adi') or
        otel_ad.lower()
        .replace(' ', '-').replace('ı','i').replace('ş','s')
        .replace('ğ','g').replace('ü','u').replace('ö','o')
        .replace('ç','c').replace('İ','i').replace('Ş','s')
        + '-yonetim'
    )[:50]

    superadmin_key = d.get('superadmin_key') or secrets.token_urlsafe(16)
    foy_bas  = int(d.get('foy_baslangic', 1))
    adis_bas = int(d.get('adisyon_baslangic', 1))

    # DB'ye kaydet (hazirlaniyor durumunda)
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO musteriler
            (otel_ad, kisa_ad, repo_adi, tur, durum, oda_sayi, foy_baslangic,
             adisyon_baslangic, sehir, superadmin_key, gmail_user, gmail_pass, yedek_mail)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            otel_ad, d.get('kisa_ad','').upper(), repo_adi, tur, 'hazirlaniyor',
            int(d.get('oda_sayi', 20)), foy_bas, adis_bas,
            d.get('sehir',''), superadmin_key,
            d.get('gmail_user', GMAIL_USER),
            d.get('gmail_pass', GMAIL_PASS),
            d.get('yedek_mail','')
        ))
        conn.commit()
        musteri_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'ok': False, 'error': f'"{repo_adi}" repo adı zaten kullanımda'})
    conn.close()

    # Arka planda oluştur
    import threading
    t = threading.Thread(target=_olustur_bg, args=(musteri_id, d, repo_adi, superadmin_key, tur, foy_bas, adis_bas))
    t.daemon = True
    t.start()

    return jsonify({'ok': True, 'musteri_id': musteri_id, 'repo_adi': repo_adi})

def _log(musteri_id, islem, sonuc, detay=''):
    conn = get_db()
    conn.execute("INSERT INTO islem_log (musteri_id, islem, sonuc, detay) VALUES (?,?,?,?)",
                 (musteri_id, islem, sonuc, detay))
    conn.commit()
    conn.close()

def _durum_guncelle(musteri_id, durum, **kwargs):
    conn = get_db()
    sets = ['durum=?']
    vals = [durum]
    for k, v in kwargs.items():
        sets.append(f'{k}=?')
        vals.append(v)
    vals.append(musteri_id)
    conn.execute(f"UPDATE musteriler SET {','.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()

def _olustur_bg(musteri_id, d, repo_adi, superadmin_key, tur, foy_bas, adis_bas):
    """GitHub repo oluştur → Render deploy → Config güncelle"""
    otel_ad = d['otel_ad'].strip()

    # 1. GitHub repo oluştur
    _log(musteri_id, 'github_repo', 'basliyor')
    try:
        req.patch(f"https://api.github.com/repos/{TEMPLATE_REPO}",
                  headers=GITHUB_HEADERS, json={"is_template": True})
        r = req.post(
            f"https://api.github.com/repos/{TEMPLATE_REPO}/generate",
            headers={**GITHUB_HEADERS, "Accept": "application/vnd.github.baptiste-preview+json"},
            json={
                "owner": GITHUB_USER, "name": repo_adi,
                "description": f"{otel_ad} — Rezervasyon ve Ön Muhasebe",
                "private": False, "include_all_branches": False
            }
        )
        if r.status_code not in (200, 201, 202):
            raise Exception(f"GitHub hata: {r.status_code} {r.text[:200]}")
        repo_url = f"https://github.com/{GITHUB_USER}/{repo_adi}"
        _log(musteri_id, 'github_repo', 'tamam', repo_url)
        _durum_guncelle(musteri_id, 'repo_olusturuldu', repo_url=repo_url)
    except Exception as e:
        _log(musteri_id, 'github_repo', 'hata', str(e))
        _durum_guncelle(musteri_id, 'hata')
        return

    # Repo hazır olana kadar bekle
    for i in range(20):
        time.sleep(5)
        r3 = req.get(f"https://api.github.com/repos/{GITHUB_USER}/{repo_adi}/contents",
                     headers=GITHUB_HEADERS)
        if r3.status_code == 200 and len(r3.json()) > 0:
            break
    else:
        _log(musteri_id, 'github_repo', 'hata', 'Repo içeriği hazırlanamadı')
        _durum_guncelle(musteri_id, 'hata')
        return

    # 2. Render servisi oluştur
    _log(musteri_id, 'render_deploy', 'basliyor')
    try:
        bilgi = {
            'otel_ad': otel_ad, 'kisa_ad': d.get('kisa_ad','').upper(),
            'superadmin_key': superadmin_key,
            'gmail_user': d.get('gmail_user', GMAIL_USER),
            'gmail_pass': d.get('gmail_pass', GMAIL_PASS),
            'yedek_mail': d.get('yedek_mail',''),
        }
        env_vars = [
            {"key": "SUPERADMIN_KEY",  "value": superadmin_key},
            {"key": "SECRET_KEY",      "value": secrets.token_urlsafe(24)},
            {"key": "GMAIL_USER",      "value": bilgi['gmail_user']},
            {"key": "GMAIL_APP_PASSWORD", "value": bilgi['gmail_pass']},
        ]
        if bilgi['yedek_mail']:
            env_vars.append({"key": "YEDEK_MAIL", "value": bilgi['yedek_mail']})

        payload = {
            "type": "web_service",
            "name": repo_adi[:50],
            "ownerId": RENDER_OWNER,
            "repo": f"https://github.com/{GITHUB_USER}/{repo_adi}",
            "branch": "main",
            "runtime": "python",
            "buildCommand": "pip install -r requirements.txt",
            "startCommand": "gunicorn app:app",
            "plan": "starter",
            "envVars": env_vars,
            "envSpecificDetails": {"pythonVersion": "3.11.0"},
            "disk": {"name": "data", "mountPath": "/data", "sizeGB": 1}
        }
        r = req.post("https://api.render.com/v1/services",
                     headers=RENDER_HEADERS, json=payload)
        if r.status_code not in (200, 201):
            raise Exception(f"Render hata: {r.status_code} {r.text[:300]}")
        data = r.json()
        servis = data.get('service', data)
        sid = servis.get('id', '')
        site_url = servis.get('serviceDetails', {}).get('url', '') or f"https://{repo_adi}.onrender.com"
        _log(musteri_id, 'render_deploy', 'tamam', site_url)
        _durum_guncelle(musteri_id, 'deploy_bekleniyor',
                        render_service_id=sid,
                        site_url=site_url,
                        sadmin_url=f"{site_url}/sadmin")
    except Exception as e:
        _log(musteri_id, 'render_deploy', 'hata', str(e))
        _durum_guncelle(musteri_id, 'hata')
        return

    # 3. Deploy bekle
    for i in range(40):
        time.sleep(10)
        r = req.get(f"https://api.render.com/v1/services/{sid}/deploys?limit=1",
                    headers=RENDER_HEADERS)
        if r.status_code == 200:
            deploys = r.json()
            if deploys:
                dep = deploys[0].get('deploy', deploys[0])
                durum = dep.get('status', '')
                if durum == 'live':
                    _log(musteri_id, 'render_deploy', 'live')
                    break
                elif durum in ('failed', 'canceled'):
                    _log(musteri_id, 'render_deploy', 'hata', durum)
                    _durum_guncelle(musteri_id, 'hata')
                    return

    # 4. config.json güncelle
    _log(musteri_id, 'config_guncelle', 'basliyor')
    try:
        r = req.get(f"https://api.github.com/repos/{GITHUB_USER}/{repo_adi}/contents/config.json",
                    headers=GITHUB_HEADERS)
        if r.status_code == 200:
            data = r.json()
            sha = data['sha']
            cfg = json.loads(base64.b64decode(data['content']).decode('utf-8'))

            cfg.setdefault('sistem', {})
            cfg['sistem']['foy_baslangic']      = foy_bas
            cfg['sistem']['adisyon_baslangic']   = adis_bas
            cfg['sistem']['lisans_aktif']        = (tur == 'aktif')
            cfg['sistem']['demo_mod']            = (tur == 'demo')
            cfg['sistem']['demo_baslangic']      = date.today().isoformat() if tur == 'demo' else ''
            cfg['sistem']['demo_sure_gun']       = 3
            cfg['otel']['ad']                    = otel_ad
            cfg['otel']['kisa_ad']               = d.get('kisa_ad','').upper()
            cfg['otel']['sehir']                 = d.get('sehir','')
            cfg['otel']['toplam_oda']            = int(d.get('oda_sayi', 20))

            yeni = base64.b64encode(
                json.dumps(cfg, ensure_ascii=False, indent=2).encode('utf-8')
            ).decode('utf-8')

            r2 = req.put(
                f"https://api.github.com/repos/{GITHUB_USER}/{repo_adi}/contents/config.json",
                headers=GITHUB_HEADERS,
                json={"message": "Kurulum: otomatik config", "content": yeni, "sha": sha}
            )
            if r2.status_code in (200, 201):
                _log(musteri_id, 'config_guncelle', 'tamam')
            else:
                _log(musteri_id, 'config_guncelle', 'uyari', str(r2.status_code))
    except Exception as e:
        _log(musteri_id, 'config_guncelle', 'uyari', str(e))

    # 5. Tamamlandı
    _durum_guncelle(musteri_id, 'aktif' if tur == 'aktif' else 'demo')
    _log(musteri_id, 'tamamlandi', 'ok', site_url)

# ── API: Durum Sorgula ────────────────────────────────────────────────────────
@app.route('/api/durum/<int:mid>')
@login_required
def api_durum(mid):
    conn = get_db()
    row = conn.execute("SELECT * FROM musteriler WHERE id=?", (mid,)).fetchone()
    loglar = [dict(r) for r in conn.execute(
        "SELECT * FROM islem_log WHERE musteri_id=? ORDER BY id DESC LIMIT 10", (mid,)
    ).fetchall()]
    conn.close()
    if not row:
        return jsonify({'ok': False})
    return jsonify({'ok': True, 'musteri': dict(row), 'loglar': loglar})

# ── API: Müşteri İşlemleri ────────────────────────────────────────────────────
@app.route('/api/musteri/<int:mid>/askiya', methods=['POST'])
@login_required
def api_askiya(mid):
    conn = get_db()
    row = conn.execute("SELECT render_service_id FROM musteriler WHERE id=?", (mid,)).fetchone()
    conn.close()
    if row and row['render_service_id']:
        req.post(f"https://api.render.com/v1/services/{row['render_service_id']}/suspend",
                 headers=RENDER_HEADERS)
    _durum_guncelle(mid, 'askida')
    return jsonify({'ok': True})

@app.route('/api/musteri/<int:mid>/aktif', methods=['POST'])
@login_required
def api_aktif(mid):
    conn = get_db()
    row = conn.execute("SELECT render_service_id FROM musteriler WHERE id=?", (mid,)).fetchone()
    conn.close()
    if row and row['render_service_id']:
        req.post(f"https://api.render.com/v1/services/{row['render_service_id']}/resume",
                 headers=RENDER_HEADERS)
    _durum_guncelle(mid, 'aktif')
    return jsonify({'ok': True})

@app.route('/api/musteri/<int:mid>/tur', methods=['POST'])
@login_required
def api_tur_degistir(mid):
    """Demo → Aktif'e yükselt"""
    d = request.get_json()
    yeni_tur = d.get('tur', 'aktif')
    conn = get_db()
    row = dict(conn.execute("SELECT * FROM musteriler WHERE id=?", (mid,)).fetchone())
    conn.close()

    # config.json'da lisans_aktif = True yap
    try:
        r = req.get(
            f"https://api.github.com/repos/{GITHUB_USER}/{row['repo_adi']}/contents/config.json",
            headers=GITHUB_HEADERS)
        if r.status_code == 200:
            data = r.json()
            cfg = json.loads(base64.b64decode(data['content']).decode('utf-8'))
            cfg['sistem']['lisans_aktif'] = (yeni_tur == 'aktif')
            cfg['sistem']['demo_mod'] = (yeni_tur == 'demo')
            yeni = base64.b64encode(json.dumps(cfg, ensure_ascii=False, indent=2).encode()).decode()
            req.put(
                f"https://api.github.com/repos/{GITHUB_USER}/{row['repo_adi']}/contents/config.json",
                headers=GITHUB_HEADERS,
                json={"message": f"Lisans: {yeni_tur}", "content": yeni, "sha": data['sha']}
            )
            # Render'da redeploy tetikle
            if row.get('render_service_id'):
                req.post(f"https://api.render.com/v1/services/{row['render_service_id']}/deploys",
                         headers=RENDER_HEADERS, json={})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

    conn = get_db()
    conn.execute("UPDATE musteriler SET tur=?, durum=? WHERE id=?", (yeni_tur, yeni_tur, mid))
    conn.commit()
    conn.close()
    _log(mid, 'tur_degistir', 'ok', yeni_tur)
    return jsonify({'ok': True})

@app.route('/api/musteri/<int:mid>/not', methods=['POST'])
@login_required
def api_not(mid):
    d = request.get_json()
    conn = get_db()
    conn.execute("UPDATE musteriler SET notlar=? WHERE id=?", (d.get('not',''), mid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/musteri/<int:mid>/sil', methods=['POST'])
@login_required
def api_sil(mid):
    conn = get_db()
    row = dict(conn.execute("SELECT * FROM musteriler WHERE id=?", (mid,)).fetchone())
    conn.close()
    # Render servisi sil
    if row.get('render_service_id'):
        req.delete(f"https://api.render.com/v1/services/{row['render_service_id']}",
                   headers=RENDER_HEADERS)
    # GitHub repo sil
    if row.get('repo_adi'):
        req.delete(f"https://api.github.com/repos/{GITHUB_USER}/{row['repo_adi']}",
                   headers=GITHUB_HEADERS)
    conn = get_db()
    conn.execute("DELETE FROM musteriler WHERE id=?", (mid,))
    conn.execute("DELETE FROM islem_log WHERE musteri_id=?", (mid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── API: Manuel Kayıt Ekle ───────────────────────────────────────────────────
@app.route('/api/manuel-ekle', methods=['POST'])
@login_required
def api_manuel_ekle():
    """Mevcut bir servisi panele kaydet (yeni oluşturmadan)."""
    d = request.get_json()
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO musteriler
            (otel_ad, kisa_ad, repo_adi, repo_url, site_url, sadmin_url,
             superadmin_key, render_service_id, tur, durum,
             oda_sayi, foy_baslangic, adisyon_baslangic, sehir, notlar)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            d['otel_ad'], d.get('kisa_ad',''), d.get('repo_adi',''),
            d.get('repo_url',''), d.get('site_url',''),
            d.get('site_url','') + '/sadmin',
            d.get('superadmin_key',''), d.get('render_service_id',''),
            d.get('tur','aktif'), d.get('durum','aktif'),
            int(d.get('oda_sayi', 20)),
            int(d.get('foy_baslangic', 1)),
            int(d.get('adisyon_baslangic', 1)),
            d.get('sehir',''), d.get('notlar','')
        ))
        conn.commit()
        mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return jsonify({'ok': True, 'id': mid})
    except sqlite3.IntegrityError as e:
        conn.close()
        return jsonify({'ok': False, 'error': str(e)})

if __name__ == '__main__':
    app.run(debug=True, port=5050)
