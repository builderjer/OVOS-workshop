import logging

try:
    from ovos_workshop.skills.ovos import MycroftSkill, OVOSSkill, OVOSFallbackSkill
    from ovos_workshop.skills.idle_display_skill import IdleDisplaySkill
except ImportError as e:
    from ovos_utils.log import LOG
    LOG.warning(e)
    if LOG.level == logging.DEBUG:
        # This can really slow down init on a Pi, only init if we're going to log
        import inspect
        from pprint import pformat
        for call in inspect.stack():
            module = inspect.getmodule(call.frame)
            name = module.__name__ if module else call.filename
            LOG.debug(f"{name}:{call.lineno}")

    # if mycroft is not available do not export the skill class
    # this is common in OvosAbstractApp implementations such as OCP

from ovos_workshop.decorators.layers import IntentLayers

