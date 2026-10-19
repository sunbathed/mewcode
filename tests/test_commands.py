from mewcode.commands import command_usage, parse_command


def test_parse_commands():
    assert parse_command("/use abc").name == "use"
    assert parse_command("/use abc").argument == "abc"
    assert parse_command("/init").name == "init"
    assert parse_command("/help").name == "help"
    assert parse_command("hello") is None


def test_command_usage():
    assert "会话ID" in command_usage(parse_command("/use"))
    assert "不需要参数" in command_usage(parse_command("/init"))
    assert "未知命令" in command_usage(parse_command("/what"))
