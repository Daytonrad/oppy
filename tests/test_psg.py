from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from opportunity_beacon.models import MatchStatus, Registration, content_hash, field_diff, match_courses
from opportunity_beacon.psg import Outcome, parse_detail, parse_listing

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / f"psg-{name}.html").read_text(encoding="utf-8")


def selection(notice):
    result = parse_listing(fixture("listing"))
    assert result.status == Outcome.OK
    return next(s for s in result.value if s.notice == notice)


def detail(number, markup=None):
    result = parse_detail(fixture(f"detail-{number}") if markup is None else markup,
                          selection(f"{number}/2026"))
    assert result.status == Outcome.OK, result.reason
    return result.value


def test_municipal_order():
    listing = fixture("listing")
    split = listing.index('<div class="panel panel-default">', listing.index("Ananindeua"))
    first = listing.index('<div class="panel panel-default">')
    last = listing.index("</main>")
    swapped = listing[:first] + listing[split:last] + listing[first:split] + listing[last:]
    before, after = parse_listing(listing), parse_listing(swapped)
    assert before == after
    assert [s.key.identifier for s in after.value] == ["948", "377342740"]


def test_absent_target_is_inconclusive():
    result = parse_listing(fixture("listing").replace("Santarém", "Belém"))
    assert result.status == Outcome.INCONCLUSIVE and result.reason and result.value is None


@pytest.mark.parametrize(("old", "new"), [
    ("panel-heading", "other-heading"),
    ("/detalhar/santarem/", "/detalhar/ananindeua/"),
    ("https://www.pa.senac.br/psg/selecao/detalhar/santarem", "https://example.org/santarem"),
    ("Ananindeua", "Santarém"),
])
def test_broken_listing(old, new):
    result = parse_listing(fixture("listing").replace(old, new))
    assert result.status == Outcome.FAILED and result.reason and result.value is None


def test_broken_detail():
    result = parse_detail(fixture("detail-126").replace("lista-psg-cursos", "other"),
                          selection("126/2026"))
    assert result.status == Outcome.FAILED and result.reason and result.value is None


def test_course_fields():
    data = detail("126")
    closed, active, full = data.courses
    assert [c.registration for c in data.courses] == [Registration.CLOSED, Registration.OPEN, Registration.FULL]
    assert full.requirements.endswith("segurança lógi")
    assert active.alias == "3587" and closed.alias is None
    assert active.registration_start.isoformat() == "2026-11-19T09:00:00-03:00"
    assert active.registration_start.tzinfo.key == "America/Santarem"
    assert active.course_start == date(2026, 12, 1)
    assert active.venue is None
    assert [d.label for d in data.documents] == ["Edital 126/2026", "Retificativo I"]


def test_in_person_does_not_imply_open_or_closed():
    course, = detail("152").courses
    assert course.registration == Registration.UNKNOWN
    assert course.registration_mode == "inscrição presencial"
    assert course.venue == "Casa Azul"


def test_hash_and_field_diff():
    markup = fixture("detail-152")
    course, = detail("152").courses
    spaced, = detail("152", markup.replace("Serviços de Saúde", "Serviços   de\n Saúde")).courses
    later, = detail("152", markup.replace("24/11/2026", "25/11/2026")).courses
    assert content_hash(course) == content_hash(spaced)
    assert field_diff(course, spaced) == {}
    assert content_hash(course) != content_hash(later)
    assert field_diff(course, later) == {
        "registration_end": ("2026-11-24T18:00:00-03:00", "2026-11-25T18:00:00-03:00")
    }


def test_unique_alias_matches_rename():
    course = detail("126").courses[1]
    matches = match_courses((course,), (replace(course, title="Revised title"),))
    assert matches[0].status == MatchStatus.MATCHED and matches[0].previous_index == 0


def test_one_to_one_composite_matching_and_collisions():
    course, = detail("152").courses
    second = replace(course, venue="Other venue")
    matches = match_courses((course, second), (second, course))
    assert [m.previous_index for m in matches] == [1, 0]
    for old, new in [((course, course), (course,)), ((course,), (course, course)),
                     ((course,), (replace(course, title="Revised title"),))]:
        assert all(m.status == MatchStatus.UNRESOLVED for m in match_courses(old, new))


def test_duplicate_alias_falls_back_to_distinct_natural_keys():
    course = replace(detail("152").courses[0], alias="repeated")
    other = replace(course, course_start=date(2027, 1, 4))
    assert [m.previous_index for m in match_courses((course, other), (other, course))] == [1, 0]


def test_missing_registration_bound_preserves_known_start():
    markup = fixture("detail-152").replace("24/11/2026", "")
    course, = detail("152", markup).courses
    assert course.registration_start.isoformat() == "2026-11-04T09:00:00-03:00"
    assert course.registration_end is None


def test_detail_cannot_be_assigned_to_another_selection():
    result = parse_detail(fixture("detail-152"), selection("126/2026"))
    assert result.status == Outcome.FAILED and result.value is None
