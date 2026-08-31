import mobase

from .no_usvfs_launch import MarvelRivalsNoUsvfsLaunchPlugin


def createPlugin() -> mobase.IPlugin:
    return MarvelRivalsNoUsvfsLaunchPlugin()
