from hdd_analyzer.config import (
    categorize,
    is_appdata_private_subtree,
    is_marker_skip,
    is_skip_dir,
    is_var_child_skip,
    should_hash_content,
)


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


def test_skip_dir_matches_temp_and_cache_anywhere():
    # Temp/Cache and friends are unconditional skips now, not just under
    # AppData, since they're junk everywhere they appear.
    assert is_skip_dir("Temp", ("users", "bob", "documents"))
    assert is_skip_dir("cache", ())


def test_skip_dir_matches_extended_dependency_caches():
    for name in (".nuget", ".gradle", ".m2", ".cargo", ".npm", ".yarn", "site-packages", ".mypy_cache", ".idea"):
        assert is_skip_dir(name, ())


def test_appdata_private_subtree_skips_local_and_locallow():
    parents = ("users", "bob", "appdata")
    assert is_appdata_private_subtree("Local", parents)
    assert is_appdata_private_subtree("LocalLow", parents)


def test_appdata_private_subtree_keeps_roaming():
    parents = ("users", "bob", "appdata")
    assert not is_appdata_private_subtree("Roaming", parents)


def test_appdata_private_subtree_requires_appdata_parent():
    assert not is_appdata_private_subtree("Local", ("users", "bob"))


def test_marker_skip_unity_library_temp_logs_obj_with_project_settings():
    siblings = frozenset({"projectsettings", "assets", "library"})
    assert is_marker_skip("Library", siblings)
    assert is_marker_skip("Temp", siblings)
    assert is_marker_skip("Logs", siblings)
    assert is_marker_skip("obj", siblings)


def test_marker_skip_unity_requires_marker_sibling():
    assert not is_marker_skip("Library", frozenset({"src", "readme.md"}))


def test_marker_skip_rust_maven_target():
    assert is_marker_skip("target", frozenset({"cargo.toml", "src"}))
    assert is_marker_skip("target", frozenset({"pom.xml"}))
    assert not is_marker_skip("target", frozenset({"src"}))


def test_marker_skip_dotnet_bin_obj_via_csproj_or_sln():
    assert is_marker_skip("bin", frozenset({"app.csproj"}))
    assert is_marker_skip("obj", frozenset({"solution.sln"}))
    assert not is_marker_skip("bin", frozenset({"readme.md"}))


def test_marker_skip_build_via_gradle_cmake_or_npm():
    assert is_marker_skip("build", frozenset({"gradlew"}))
    assert is_marker_skip("build", frozenset({"cmakelists.txt"}))
    assert is_marker_skip("build", frozenset({"package.json"}))
    assert not is_marker_skip("build", frozenset({"readme.md"}))


def test_marker_skip_js_output_dirs_via_package_json():
    for name in ("dist", ".next", ".nuxt", "coverage"):
        assert is_marker_skip(name, frozenset({"package.json"}))
    assert not is_marker_skip("dist", frozenset({"readme.md"}))


def test_marker_skip_vendor_via_composer_or_go_mod():
    assert is_marker_skip("vendor", frozenset({"composer.json"}))
    assert is_marker_skip("vendor", frozenset({"go.mod"}))
    assert not is_marker_skip("vendor", frozenset({"readme.md"}))


def test_should_hash_content_text_code_doc_under_limit():
    assert should_hash_content("text", 100)
    assert should_hash_content("code", 100)
    assert should_hash_content("doc", 50 * 1024 * 1024)


def test_should_hash_content_false_over_size_limit():
    assert not should_hash_content("text", 50 * 1024 * 1024 + 1)


def test_should_hash_content_false_for_other_categories():
    for category in ("image", "archive", "av", "binary"):
        assert not should_hash_content(category, 100)


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
