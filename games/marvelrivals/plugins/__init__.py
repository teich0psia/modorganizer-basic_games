import mobase

from .full_deployment import MarvelRivalsFullDeploymentPlugin
from .no_usvfs_launch import MarvelRivalsNoUsvfsLaunchPlugin


def createPlugins() -> list[mobase.IPlugin]:
    return [
        MarvelRivalsNoUsvfsLaunchPlugin(),
        MarvelRivalsFullDeploymentPlugin(),
    ]
