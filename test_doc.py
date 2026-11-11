"""Word 97-2003 .doc support (fixtures made with LibreOffice from synthetic text)."""
import io
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

import app as web
from extract import ExtractError, extract_text

FIX = Path(__file__).parent / 'tests' / 'fixtures'
EXPECTED = {'en': ('Remittances and household welfare in rural Uzbekistan', 'substantially increased educational expenditure'),
            'ru': ('Цифровизация высшего образования в Узбекистане', 'дистанционного обучения существенно изменило'),
            'uzlat': ('Davlat qarzi barqarorligini ta’minlash masalalari', 'Oʻzbekiston Respublikasida davlat qarzi'),
            'uzcyr': ('Давлат қарзи барқарорлигини таъминлаш масалалари', 'Ўзбекистон Республикасида давлат қарзи')}


@pytest.mark.parametrize('name', EXPECTED)
def test_doc_text_in_order_with_tables_and_fields(name):
    text = extract_text(name + '.doc', (FIX / f'{name}.doc').read_bytes())
    title, phrase = EXPECTED[name]
    assert text.startswith(title) and phrase in text
    assert 'PAGE' not in text                                  # field code removed, result kept
    assert 'Cell one\tCell two\n' in text                      # table row ends on a new line
    assert text.rstrip().endswith('Final sentence.')
    assert not any(ord(c) < 32 and c not in '\n\t' for c in text)


def test_renamed_docx_and_fake_doc_files():
    doc = Document()
    doc.add_paragraph('A modern Word file that was renamed to the old extension by its owner.')
    buf = io.BytesIO()
    doc.save(buf)
    assert extract_text('old.doc', buf.getvalue()).startswith('A modern Word file')
    for fake in (b'{\\rtf1\\ansi Some text}', b'<html><body>Saved as web page</body></html>'):
        with pytest.raises(ExtractError) as info:
            extract_text('x.doc', fake)
        assert info.value.code == 'doc_not_word'
    with pytest.raises(ExtractError) as info:
        extract_text('x.doc', b'\xd0\xcf\x11\xe0 broken container' * 20)
    assert info.value.code == 'extract_failed'


def test_doc_upload_checks_and_quotes():
    web.RATE.clear(); web.results.clear(); web.QUOTES.clear()
    client = TestClient(web.app)
    content = (FIX / 'uzcyr.doc').read_bytes()
    data = client.post('/api/check', data={'mode': 'reference', 'language': 'uz',
                                           'reference_text': "O‘zbekiston Respublikasida davlat qarzi barqarorligini ta’minlashning "
                                                             "nazariy va amaliy jihatlari tahlil qilingan."},
                       files={'submission': ('maqola.doc', content)}).json()
    assert data['percent'] > 30 and data['document'] == 'maqola.doc'      # Cyrillic .doc vs Latin reference
    quote = client.post('/api/quote', files={'submission': ('maqola.doc', content)}).json()
    assert quote['words'] > 20 and quote['id'] == 'free'


def test_compressed_cp1252_pieces(tmp_path):
    """Word stores Latin-only text as 8-bit (cp1252) pieces; rewrite the English fixture that way."""
    import shutil
    import struct
    import olefile
    path = tmp_path / 'compressed.doc'
    shutil.copy(FIX / 'en.doc', path)
    ole = olefile.OleFileIO(str(path), write_mode=True)
    word = bytearray(ole.openstream('WordDocument').read())
    flags = struct.unpack_from('<HH6xH', word, 0)[2]
    table_name = '1Table' if flags & 0x0200 else '0Table'
    table = bytearray(ole.openstream(table_name).read())
    fc_clx, _ = struct.unpack_from('<II', word, 0x01A2)
    pos = fc_clx
    while table[pos] == 0x01:
        pos += 3 + struct.unpack_from('<h', table, pos + 1)[0]
    lcb = struct.unpack_from('<I', table, pos + 1)[0]
    plc = pos + 5
    pieces = (lcb - 4) // 12
    start, end = struct.unpack_from('<II', table, plc)
    pcd = plc + 4 * (pieces + 1)
    offset = struct.unpack_from('<I', table, pcd + 2)[0] & 0x3FFFFFFF
    text = word[offset:offset + 2 * (end - start)].decode('utf-16-le').replace('rural', 'rural’s')[:end - start]
    encoded = text.encode('cp1252')
    word[offset:offset + len(encoded)] = encoded                         # 8-bit text at the same place
    struct.pack_into('<I', table, pcd + 2, (offset * 2) | 0x40000000)  # fc for compressed piece
    ole.write_stream('WordDocument', bytes(word))
    ole.write_stream(table_name, bytes(table))
    ole.close()
    result = extract_text('compressed.doc', path.read_bytes())
    assert result.startswith('Remittances and household welfare in rural’s Uzbekistan')
    assert 'substantially increased educational expenditure' in result
