"""Unit tests for registration field validators (no database needed)."""
import pytest

from apps.registry import validation as v


def ok(fn, value, **kw):
    return fn(value, **kw).value


def err(fn, value, **kw):
    with pytest.raises(v.FieldError) as exc:
        fn(value, **kw)
    return exc.value


# --- Name ---

@pytest.mark.parametrize("name", ["Janavi T", "Amrita Zen A", "Sasi Kala Sri R", "D'Souza Anne-Marie",
                                  "R.K. Priya", "Test Kumar L", "Sample B M", "Ó Briain", "Sai"])
def test_valid_names(name):
    assert ok(v.check_person_name, name) == name


def test_name_whitespace_is_collapsed_not_respelled():
    assert ok(v.check_person_name, "  Sasi  Kala   Sri R ") == "Sasi Kala Sri R"


@pytest.mark.parametrize("raw,suggestion", [
    ("amrita zen a", "Amrita Zen A"),
    ("JANAVI T", "Janavi T"),
    ("sasi Kala sri r", "Sasi Kala Sri R"),
])
def test_bad_capitalization_gets_suggestion_not_silent_fix(raw, suggestion):
    e = err(v.check_person_name, raw)
    assert "capitalize" in e.messages[0]
    assert "Amrita Zen A" in e.messages[0]  # the example from the office brief
    assert e.suggestion == suggestion


@pytest.mark.parametrize("raw", ["Priya 2", "Priya@", "", "   ", "A" * 101, "Priya_K"])
def test_invalid_names(raw):
    err(v.check_person_name, raw)


# --- Mobile ---

@pytest.mark.parametrize("raw", ["9876543210", "6000000000", "7012345678", "8123456789"])
def test_valid_mobiles(raw):
    assert ok(v.check_indian_mobile, raw) == raw


@pytest.mark.parametrize("raw,fragment", [
    ("987654321", "exactly 10 digits"),
    ("98765432101", "exactly 10 digits"),
    ("+919876543210", "digits only"),
    ("98765 43210", "digits only"),
    ("98765-43210", "digits only"),
    ("98765abcde", "digits only"),
    ("０９８７６５４３２１", "digits only"),  # full-width digits
    ("5876543210", "start with 6, 7, 8 or 9"),
    ("0987654321", "start with 6, 7, 8 or 9"),
    ("", "enter your mobile"),
])
def test_invalid_mobiles(raw, fragment):
    assert fragment in err(v.check_indian_mobile, raw).messages[0]


# --- Email ---

def test_gmail_only_accepts_gmail_and_normalizes_case():
    assert ok(v.check_email, "  Student.Name@Gmail.COM ", gmail_only=True) == "student.name@gmail.com"


def test_gmail_dots_and_plus_are_not_folded():
    assert ok(v.check_email, "a.b+x@gmail.com", gmail_only=True) == "a.b+x@gmail.com"


@pytest.mark.parametrize("raw", ["student@yahoo.com", "student@googlemail.com", "student@gmail.co",
                                 "student@college.edu.in"])
def test_gmail_only_rejects_other_providers(raw):
    assert "Gmail" in err(v.check_email, raw, gmail_only=True).messages[0]


def test_non_gmail_allowed_when_policy_off():
    assert ok(v.check_email, "student@college.edu.in", gmail_only=False) == "student@college.edu.in"


@pytest.mark.parametrize("raw", ["not-an-email", "a@", "@gmail.com", "a b@gmail.com", "a@@gmail.com"])
def test_invalid_email_syntax(raw):
    assert "valid" in err(v.check_email, raw, gmail_only=True).messages[0]


# --- Registration number ---

@pytest.mark.parametrize("raw", ["219X1B07C4", "100200300400", "55500011", "24MCA100200", "21-CS-045", "UG/2023/114"])
def test_valid_reg_nos(raw):
    assert ok(v.check_reg_no, raw) == raw


@pytest.mark.parametrize("raw", ["", "#123", "12;DROP", "A" * 41])
def test_invalid_reg_nos(raw):
    err(v.check_reg_no, raw)


# --- Degree ---

@pytest.mark.parametrize("raw", [
    "B.E. (Computer Science and Engineering)", "B.E. (Electrical and Electronics Engineering)",
    "B.Tech. (Information Technology)", "B.Tech (Computer Science and Engineering)", "M.Tech. (Computer Science)",
    "M.E. (Computer Science and Engineering)", "B.Sc., Information Technology", "B.Sc. (Computer Science)",
    "M.Sc. (Computer Science)", "M.Sc., (Computer Science)", "MCA (Computer Application)", "MCA (General)",
    "MBA (General)", "B.Tech. (AI and Data Science)", "B.E. (CSE)",
])
def test_valid_degrees_preserved_exactly(raw):
    result = v.check_degree(raw)
    assert result.value == raw
    assert result.review_notes == []


@pytest.mark.parametrize("raw", ["BCA", "BBA", "B.Com.", "M.Com."])
def test_degree_without_specialization_accepted_with_review_note(raw):
    result = v.check_degree(raw)
    assert result.value == raw
    assert any("specialization" in n for n in result.review_notes)


def test_unrecognized_degree_is_accepted_for_hr_review():
    result = v.check_degree("B.Lib.Sc. (Library Science)")
    assert result.value == "B.Lib.Sc. (Library Science)"
    assert any("not in the recognized list" in n for n in result.review_notes)


@pytest.mark.parametrize("raw,suggestion", [
    ("mca (general)", "MCA (General)"),
    ("b.e. (computer science and engineering)", "B.E. (Computer Science and Engineering)"),
    ("Mca (Computer Application)", "MCA (Computer Application)"),
    ("B.Sc., information technology", "B.Sc., Information Technology"),
])
def test_degree_capitalization_errors_suggest(raw, suggestion):
    assert err(v.check_degree, raw).suggestion == suggestion


@pytest.mark.parametrize("raw", ["B.E. (Computer Science", "B.E. [CSE]", "", "B.E. <script>"])
def test_invalid_degree_format(raw):
    err(v.check_degree, raw)


# --- College and location ---

@pytest.mark.parametrize("raw", [
    "Dr. Mahalingam College of Engineering and Technology", "PSG College of Technology",
    "Example Institute of Engineering and Technology", "The Example University", "Sample University",
    "Government Science College (Autonomous)", "St. Joseph's College of Engineering", "SRM Institute of Science and Technology",
])
def test_valid_college_names(raw):
    assert ok(v.check_college_name, raw) == raw


def test_college_lowercase_words_get_suggestion():
    e = err(v.check_college_name, "government science college (Autonomous)")
    assert e.suggestion == "Government Science College (Autonomous)"


def test_college_all_caps_gets_suggestion_keeping_short_acronyms():
    e = err(v.check_college_name, "PSG COLLEGE OF TECHNOLOGY")
    assert e.suggestion == "PSG College of Technology"


@pytest.mark.parametrize("raw", ["Pollachi", "Testville", "Sampleton", "New Delhi", "Tiruchirappalli"])
def test_valid_locations(raw):
    assert ok(v.check_location, raw) == raw


@pytest.mark.parametrize("raw", ["pollachi", "Testville 123456", ""])
def test_invalid_locations(raw):
    err(v.check_location, raw)


# --- Project title ---

@pytest.mark.parametrize("raw", [
    "Hotel Management System",
    "AI Demand Forecaster using News NLP Weather Data",
    "IoT Based Smart Irrigation with YOLOv8",
    "eCommerce Site for iOS Users",
    "Student Portal in ASP.NET",
    "C++ Library Management System",
    "Sentiment Analysis of Tweets: A BERT-based Approach",
    "Covid-19 Data Dashboard",
])
def test_valid_project_titles_preserve_acronyms_and_terms(raw):
    assert ok(v.check_project_title, raw) == raw


def test_project_title_lowercase_gets_suggestion():
    e = err(v.check_project_title, "hotel management system")
    assert e.suggestion == "Hotel Management System"


def test_project_title_connecting_words_may_be_lowercase():
    assert ok(v.check_project_title, "Analysis of Sales and Profit for Retail") == \
        "Analysis of Sales and Profit for Retail"


@pytest.mark.parametrize("raw", ["", "AB", "X" * 201, "Hotel <b>System</b>"])
def test_invalid_project_titles(raw):
    err(v.check_project_title, raw)
