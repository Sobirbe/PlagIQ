from app import compare, tokens

def test_uzbek_latin_cyrillic():
    assert tokens("Олий таълим муассасаларида сифат муҳим", "uz") == tokens("Oliy ta'lim muassasalarida sifat muhim", "uz")

def test_exact_seven_token_match():
    result = compare("one two three four five six seven eight nine", "zero one two three four five six seven extra", "en")
    assert result["matched_tokens"] == 7

def test_short_text_returns_zero():
    assert compare("hello", "hello", "en")["percent"] == 0

def test_russian_cyrillic_maintained():
    assert tokens("Школа", "ru") == ["школа"]
