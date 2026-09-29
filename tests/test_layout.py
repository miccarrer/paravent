from paravent import layout

CHAR = 10  # width of a character, in the boxes below


def row(text, number, left=0, height=20, pitch=30, score=None):
    """A piece of text on the ``number``-th row, one character CHAR wide."""
    top = number * pitch
    return layout.Piece(text, left, top, left + CHAR * len(text), top + height, score)


def test_wrapped_rows_form_one_paragraph():
    pieces = [row("Nous avons bien reçu votre demande et nous", 0),
              row("la traitons dans les meilleurs délais.", 1),
              row("Cordialement,", 3)]  # a blank row above
    assert layout.paragraphs(pieces) == ("Nous avons bien reçu votre demande et nous la traitons dans les"
                                         " meilleurs délais.\n\nCordialement,")


def test_short_rows_stay_apart():
    pieces = [row("Nous avons bien reçu votre demande et nous", 0),
              row("Madame Exemple", 2), row("12, rue des Écoles", 3), row("99000 Exempleville", 4)]
    assert layout.paragraphs(pieces).split("\n\n")[1:] == ["Madame Exemple", "12, rue des Écoles", "99000 Exempleville"]


def test_list_items_fields_and_titles_start_their_own_block():
    full = "des mots ordinaires qui remplissent la ligne"
    pieces = [row(full, 0), row("– un élément", 1),
              row(full, 2), row("Numéro allocataire : 1234567", 3),
              row(full, 4), row("TITRE", 5, height=40)]
    assert layout.paragraphs(pieces).count("\n\n") == 5


def test_a_sentence_ending_with_room_to_spare_ends_the_paragraph():
    pieces = [row("Votre dossier est complet. Il sera examiné", 0),
              row("sous huit jours par nos services.", 1),
              row("Le montant est maintenu.", 2)]
    assert layout.paragraphs(pieces).split("\n\n") == [
        "Votre dossier est complet. Il sera examiné sous huit jours par nos services.", "Le montant est maintenu."]


def test_hyphen_at_the_end_of_a_row_is_kept_without_a_space():
    pieces = [row("à hauteur de soixante-dix pour cent, soixante-", 0), row("dix euros", 1)]
    assert layout.paragraphs(pieces) == "à hauteur de soixante-dix pour cent, soixante-dix euros"


def test_table_rows_are_read_left_to_right_and_never_joined():
    # The amounts reach the right edge on every row: without the column gap, one paragraph.
    pieces = [row("Date", 0), row("Libellé", 0, left=100), row("Montant", 0, left=400),
              row("3 mars", 1), row("pharmacie", 1, left=100), row("12,50 €", 1, left=400),
              row("5 mars", 2), row("médecin", 2, left=100), row("25,00 €", 2, left=400)]
    assert layout.paragraphs(pieces) == "Date Libellé Montant\n\n3 mars pharmacie 12,50 €\n\n5 mars médecin 25,00 €"


def test_render_numbers_pieces_in_reading_order():
    pieces = [row("deuxième", 2), row("premier", 0)]
    assert layout.paragraphs(pieces, lambda piece, number: f"{number}:{piece.text}") == "1:premier\n\n2:deuxième"


def test_nothing_to_rebuild():
    assert layout.paragraphs([]) == ""


def test_an_address_on_the_right_keeps_its_lines_apart():
    # Its lines reach the right edge of the page, like full rows of a paragraph would.
    pieces = [row("Caisse Exemple de Retraite, service des paiements", 0),
              row("Madame Élodie Lefèvre", 2, left=300), row("12, rue des Écoles", 3, left=300),
              row("Nous avons bien reçu votre demande et nous la", 5), row("traitons au plus vite.", 6)]
    assert layout.paragraphs(pieces).split("\n\n") == [
        "Caisse Exemple de Retraite, service des paiements", "Madame Élodie Lefèvre", "12, rue des Écoles",
        "Nous avons bien reçu votre demande et nous la traitons au plus vite."]
