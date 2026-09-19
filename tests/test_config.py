from hdd_analyzer.config import categorize, is_skip_dir, is_var_child_skip


def test_categorize_text_extension():
    assert categorize("txt") == "text"
    assert categorize("MD") == "text"


def test_categorize_code_extension():
    assert categorize("py") == "code"
    assert categorize("RS") == "code"


def test_categorize_doc_extension():
    assert categorize("pdf") == "doc"
    assert categorize("docx") == "doc"


def test_categorize_unknown_extension_is_binary():
    assert categorize("exe") == "binary"
    assert categorize("") == "binary"


def test_skip_dir_matches_common_system_dirs():
    assert is_skip_dir("Windows", ())
    assert is_skip_dir("node_modules", ())
    assert is_skip_dir(".git", ())
    assert is_skip_dir("$Recycle.Bin", ())


def test_skip_dir_case_insensitive():
    assert is_skip_dir("WINDOWS", ())
    assert is_skip_dir("Program Files (x86)", ())


def test_skip_dir_does_not_match_ordinary_dirs():
    assert not is_skip_dir("Documents", ())
    assert not is_skip_dir("Photos", ())


def test_skip_dir_matches_appdata_local_temp_cache():
    parents = ("users", "bob", "appdata", "local")
    assert is_skip_dir("Temp", parents)
    assert is_skip_dir("Cache", parents)


def test_skip_dir_does_not_match_temp_outside_appdata():
    assert not is_skip_dir("Temp", ("users", "bob", "documents"))


def test_skip_dir_linux_system_dirs():
    assert is_skip_dir("proc", ())
    assert is_skip_dir("usr", ())
    assert is_skip_dir("lost+found", ())


def test_skip_dir_keeps_etc_opt_srv():
    assert not is_skip_dir("etc", ())
    assert not is_skip_dir("opt", ())
    assert not is_skip_dir("srv", ())


def test_var_child_skip_excludes_mail():
    assert is_var_child_skip("log", ("var",))
    assert not is_var_child_skip("mail", ("var",))


def test_var_child_skip_ignores_unrelated_parent():
    assert not is_var_child_skip("log", ("home", "bob"))
