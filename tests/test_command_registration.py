from endstone_endkeep.plugin import EndKeepPlugin


def test_maintenance_command_uses_each_enum_type_once() -> None:
    usages = EndKeepPlugin.commands["backup"]["usages"]

    assert usages == [
        "/backup (status|create|list|verify|reload)<action: EndKeepBackupAction>",
        "/backup (maintenance)<action: EndKeepMaintenanceAction> (full)[mode: EndKeepMaintenanceMode]",
    ]

    joined = "\n".join(usages)
    assert joined.count("EndKeepMaintenanceAction") == 1
    assert joined.count("EndKeepMaintenanceMode") == 1
