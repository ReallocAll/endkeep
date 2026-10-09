from endstone_endkeep.plugin import EndKeepPlugin


def test_backup_command_uses_each_enum_type_once() -> None:
    usages = EndKeepPlugin.commands["backup"]["usages"]

    assert usages == [
        "/backup (status|create|list|cancel|reload|confirm)<action: EndKeepBackupAction>",
        "/backup (verify)<action: EndKeepVerifyAction> (deep)[mode: EndKeepVerifyMode]",
        "/backup (maintenance)<action: EndKeepMaintenanceAction> (full)[mode: EndKeepMaintenanceMode]",
        "/backup (delete|rollover)<action: EndKeepMutationAction> <snapshot: str>",
        "/backup (export)<action: EndKeepExportAction> <snapshot: str>",
    ]

    joined = "\n".join(usages)
    for enum_name in (
        "EndKeepBackupAction",
        "EndKeepVerifyAction",
        "EndKeepVerifyMode",
        "EndKeepMaintenanceAction",
        "EndKeepMaintenanceMode",
        "EndKeepMutationAction",
        "EndKeepExportAction",
    ):
        assert joined.count(enum_name) == 1
