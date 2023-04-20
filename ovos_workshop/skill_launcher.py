import gc
import importlib
import os
from os.path import isdir
import sys
from inspect import isclass
from os import path, makedirs
from time import time

from ovos_bus_client.client import MessageBusClient
from ovos_bus_client.message import Message
from ovos_config.config import Configuration
from ovos_config.locations import get_xdg_data_dirs, get_xdg_data_save_path
from ovos_plugin_manager.skills import find_skill_plugins
from ovos_utils import wait_for_exit_signal
from ovos_utils.file_utils import FileWatcher
from ovos_utils.log import LOG
from ovos_utils.process_utils import RuntimeRequirements

from ovos_workshop.skills.active import ActiveSkill
from ovos_workshop.skills.auto_translatable import UniversalSkill, UniversalFallback
from ovos_workshop.skills.base import BaseSkill
from ovos_workshop.skills.common_play import OVOSCommonPlaybackSkill
from ovos_workshop.skills.common_query_skill import CommonQuerySkill
from ovos_workshop.skills.fallback import FallbackSkill
from ovos_workshop.skills.mycroft_skill import MycroftSkill
from ovos_workshop.skills.ovos import OVOSSkill, OVOSFallbackSkill

SKILL_BASE_CLASSES = [
    BaseSkill, MycroftSkill, OVOSSkill, OVOSFallbackSkill,
    OVOSCommonPlaybackSkill, OVOSFallbackSkill, CommonQuerySkill, ActiveSkill,
    FallbackSkill, UniversalSkill, UniversalFallback
]

SKILL_MAIN_MODULE = '__init__.py'


def get_skill_directories(conf=None):
    """ returns list of skill directories ordered by expected loading order

    This corresponds to:
    - XDG_DATA_DIRS
    - user defined extra directories

    Each directory contains individual skill folders to be loaded

    If a skill exists in more than one directory (same folder name) previous instances will be ignored
        ie. directories at the end of the list have priority over earlier directories

    NOTE: empty folders are interpreted as disabled skills

    new directories can be defined in mycroft.conf by specifying a full path
    each extra directory is expected to contain individual skill folders to be loaded

    the xdg folder name can also be changed, it defaults to "skills"
        eg. ~/.local/share/mycroft/FOLDER_NAME

    {
        "skills": {
            "directory": "skills",
            "extra_directories": ["path/to/extra/dir/to/scan/for/skills"]
        }
    }

    Args:
        conf (dict): mycroft.conf dict, will be loaded automatically if None
    """
    # the contents of each skills directory must be individual skill folders
    # we are still dependent on the mycroft-core structure of skill_id/__init__.py

    conf = conf or Configuration()
    folder = conf["skills"].get("directory")

    # load all valid XDG paths
    # NOTE: skills are actually code, but treated as user data!
    # they should be considered applets rather than full applications
    skill_locations = list(reversed(
        [os.path.join(p, folder) for p in get_xdg_data_dirs()]
    ))

    # load additional explicitly configured directories
    conf = conf.get("skills") or {}
    # extra_directories is a list of directories containing skill subdirectories
    # NOT a list of individual skill folders
    skill_locations += conf.get("extra_directories") or []
    return skill_locations


def get_default_skills_directory():
    """ return default directory to scan for skills

    data_dir is always XDG_DATA_DIR
    If xdg is disabled then data_dir by default corresponds to /opt/mycroft

    users can define the data directory in mycroft.conf
    the skills folder name (relative to data_dir) can also be defined there

    NOTE: folder name also impacts all XDG skill directories!

    {
        "skills": {
            "directory_override": "/opt/mycroft/hardcoded_path/skills"
        }
    }

    Args:
        conf (dict): mycroft.conf dict, will be loaded automatically if None
    """
    folder = Configuration()["skills"].get("directory")
    skills_folder = os.path.join(get_xdg_data_save_path(), folder)
    # create folder if needed
    makedirs(skills_folder, exist_ok=True)
    return path.expanduser(skills_folder)


def remove_submodule_refs(module_name):
    """Ensure submodules are reloaded by removing the refs from sys.modules.

    Python import system puts a reference for each module in the sys.modules
    dictionary to bypass loading if a module is already in memory. To make
    sure skills are completely reloaded these references are deleted.

    Args:
        module_name: name of skill module.
    """
    submodules = []
    LOG.debug(f'Skill module: {module_name}')
    # Collect found submodules
    for m in sys.modules:
        if m.startswith(module_name + '.'):
            submodules.append(m)
    # Remove all references them to in sys.modules
    for m in submodules:
        LOG.debug(f'Removing sys.modules ref for {m}')
        del sys.modules[m]


def load_skill_module(path, skill_id):
    """Load a skill module

    This function handles the differences between python 3.4 and 3.5+ as well
    as makes sure the module is inserted into the sys.modules dict.

    Args:
        path: Path to the skill main file (__init__.py)
        skill_id: skill_id used as skill identifier in the module list
    """
    module_name = skill_id.replace('.', '_')

    remove_submodule_refs(module_name)

    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def get_skill_class(skill_module):
    """Find MycroftSkill based class in skill module.

    Arguments:
        skill_module (module): module to search for Skill class

    Returns:
        (MycroftSkill): Found subclass of MycroftSkill or None.
    """
    if callable(skill_module):
        # it's a skill plugin
        # either a func that returns the skill or the skill class itself
        return skill_module

    candidates = []
    for name, obj in skill_module.__dict__.items():
        if isclass(obj):
            if any(issubclass(obj, c) for c in SKILL_BASE_CLASSES) and \
                    not any(obj is c for c in SKILL_BASE_CLASSES):
                candidates.append(obj)

    for candidate in list(candidates):
        others = [clazz for clazz in candidates if clazz != candidate]
        # if we found a subclass of this candidate, it is not the final skill
        if any(issubclass(clazz, candidate) for clazz in others):
            candidates.remove(candidate)

    if candidates:
        if len(candidates) > 1:
            LOG.warning(f"Multiple skills found in a single file!\n"
                        f"{candidates}")
        LOG.debug(f"Loading skill class: {candidates[0]}")
        return candidates[0]
    return None


def get_create_skill_function(skill_module):
    """Find create_skill function in skill module.

    Arguments:
        skill_module (module): module to search for create_skill function

    Returns:
        (function): Found create_skill function or None.
    """
    if hasattr(skill_module, "create_skill") and \
            callable(skill_module.create_skill):
        return skill_module.create_skill
    return None


class SkillLoader:
    def __init__(self, bus, skill_directory=None, skill_id=None):
        self.bus = bus
        self._skill_directory = skill_directory
        self._skill_id = skill_id
        self._skill_class = None
        self._loaded = None
        self.load_attempted = False
        self.last_loaded = 0
        self.instance: BaseSkill = None
        self.active = True
        self._watchdog = None
        self.config = Configuration()
        self.skill_module = None

    @property
    def loaded(self):
        return self._loaded  # or self.instance is None

    @loaded.setter
    def loaded(self, val):
        self._loaded = val

    @property
    def skill_directory(self):
        skill_dir = self._skill_directory
        if self.instance and not skill_dir:
            skill_dir = self.instance.root_dir
        return skill_dir

    @skill_directory.setter
    def skill_directory(self, val):
        self._skill_directory = val

    @property
    def skill_id(self):
        skill_id = self._skill_id
        if self.instance and not skill_id:
            skill_id = self.instance.skill_id
        if self.skill_directory and not skill_id:
            skill_id = os.path.basename(self.skill_directory)
        return skill_id

    @skill_id.setter
    def skill_id(self, val):
        self._skill_id = val

    @property
    def skill_class(self):
        skill_class = self._skill_class
        if self.instance and not skill_class:
            skill_class = self.instance.__class__
        if self.skill_module and not skill_class:
            skill_class = get_skill_class(self.skill_module)
        return skill_class

    @skill_class.setter
    def skill_class(self, val):
        self._skill_class = val

    @property
    def runtime_requirements(self):
        if not self.skill_class:
            return RuntimeRequirements()
        return self.skill_class.runtime_requirements

    @property
    def is_blacklisted(self):
        """Boolean value representing whether or not a skill is blacklisted."""
        blacklist = self.config['skills'].get('blacklisted_skills') or []
        if self.skill_id in blacklist:
            return True
        else:
            return False

    @property
    def reload_allowed(self):
        return self.active and (self.instance is None or self.instance.reload_skill)

    def reload(self):
        LOG.info(f'ATTEMPTING TO RELOAD SKILL: {self.skill_id}')
        if self.instance:
            if not self.instance.reload_skill:
                LOG.info("skill does not allow reloading!")
                return False  # not allowed
            self._unload()
        return self._load()

    def load(self):
        LOG.info(f'ATTEMPTING TO LOAD SKILL: {self.skill_id}')
        return self._load()

    def _unload(self):
        """Remove listeners and stop threads before loading"""
        if self._watchdog:
            self._watchdog.shutdown()
            self._watchdog = None

        self._execute_instance_shutdown()
        if self.config.get("debug", False):
            self._garbage_collect()
        self._emit_skill_shutdown_event()

    def unload(self):
        if self.instance:
            self._execute_instance_shutdown()

    def activate(self):
        self.active = True
        self.load()

    def deactivate(self):
        self.active = False
        self.unload()

    def _execute_instance_shutdown(self):
        """Call the shutdown method of the skill being reloaded."""
        try:
            self.instance.default_shutdown()
        except Exception:
            LOG.exception(f'An error occurred while shutting down {self.skill_id}')
        else:
            LOG.info(f'Skill {self.skill_id} shut down successfully')
        del self.instance
        self.instance = None

    def _garbage_collect(self):
        """Invoke Python garbage collector to remove false references"""
        gc.collect()
        # Remove two local references that are known
        refs = sys.getrefcount(self.instance) - 2
        if refs > 0:
            LOG.warning(
                f"After shutdown of {self.skill_id} there are still {refs} references "
                "remaining. The skill won't be cleaned from memory."
            )

    def _emit_skill_shutdown_event(self):
        message = Message("mycroft.skills.shutdown",
                          {"path": self.skill_directory, "id": self.skill_id})
        self.bus.emit(message)

    def _load(self):
        self._prepare_for_load()
        if self.is_blacklisted:
            self._skip_load()
        else:
            self.skill_module = self._load_skill_source()
            self.loaded = self._create_skill_instance()

        self.last_loaded = time()
        self._communicate_load_status()
        self._start_filewatcher()
        return self.loaded

    def _start_filewatcher(self):
        if not self._watchdog:
            self._watchdog = FileWatcher([self.skill_directory],
                                         callback=self._handle_filechange,
                                         recursive=True)

    def _handle_filechange(self):
        LOG.info("Skill change detected!")
        try:
            if self.reload_allowed:
                self.reload()
        except Exception:
            LOG.exception(f'Unhandled exception occurred while reloading {self.skill_directory}')

    def _prepare_for_load(self):
        self.load_attempted = True
        self.instance = None

    def _skip_load(self):
        LOG.info(f'Skill {self.skill_id} is blacklisted - it will not be loaded')

    def _load_skill_source(self):
        """Use Python's import library to load a skill's source code."""
        main_file_path = os.path.join(self.skill_directory, SKILL_MAIN_MODULE)
        skill_module = None
        if not os.path.exists(main_file_path):
            LOG.error(f'Failed to load {self.skill_id} due to a missing file.')
        else:
            try:
                skill_module = load_skill_module(main_file_path, self.skill_id)
            except Exception as e:
                LOG.exception(f'Failed to load skill: {self.skill_id} ({e})')
        return skill_module

    def _create_skill_instance(self, skill_module=None):
        """create the skill object.

        Arguments:
            skill_module (module): Module to load from

        Returns:
            (bool): True if skill was loaded successfully.
        """
        skill_module = skill_module or self.skill_module
        try:
            skill_creator = get_create_skill_function(skill_module) or \
                            self.skill_class

            # create the skill
            # if the signature supports skill_id and bus pass them
            # to fully initialize the skill in 1 go
            try:
                # many skills do not expose this, if they don't allow bus/skill_id kwargs
                # in __init__ we need to manually call _startup
                self.instance = skill_creator(bus=self.bus,
                                              skill_id=self.skill_id)
                # skills will have bus and skill_id available as soon as they call super()
            except:
                self.instance = skill_creator()

            if hasattr(self.instance, "is_fully_initialized"):
                LOG.warning(f"Deprecated skill signature! Skill class should be"
                            f" imported from `ovos_workshop.skills`")
                is_initialized = self.instance.is_fully_initialized
            else:
                is_initialized = self.instance._is_fully_initialized
            if not is_initialized:
                # finish initialization of skill class
                self.instance._startup(self.bus, self.skill_id)
        except Exception as e:
            LOG.exception(f'Skill __init__ failed with {e}')
            self.instance = None

        return self.instance is not None

    def _communicate_load_status(self):
        if self.loaded:
            message = Message('mycroft.skills.loaded',
                              {"path": self.skill_directory,
                               "id": self.skill_id,
                               "name": self.instance.name})
            self.bus.emit(message)
            LOG.info(f'Skill {self.skill_id} loaded successfully')
        else:
            message = Message('mycroft.skills.loading_failure',
                              {"path": self.skill_directory, "id": self.skill_id})
            self.bus.emit(message)
            if not self.is_blacklisted:
                LOG.error(f'Skill {self.skill_id} failed to load')
            else:
                LOG.info(f'Skill {self.skill_id} not loaded')


class PluginSkillLoader(SkillLoader):
    def __init__(self, bus, skill_id):
        super().__init__(bus, skill_id=skill_id)

    def load(self, skill_class):
        LOG.info('ATTEMPTING TO LOAD PLUGIN SKILL: ' + self.skill_id)
        self._skill_class = skill_class
        self._prepare_for_load()
        if self.is_blacklisted:
            self._skip_load()
        else:
            self.loaded = self._create_skill_instance()

        self.last_loaded = time()
        self._communicate_load_status()
        return self.loaded


def launch_plugin_skill(skill_id):
    """ run a plugin skill standalone """
    bus = MessageBusClient()
    bus.run_in_thread()
    plugins = find_skill_plugins()
    if skill_id not in plugins:
        raise ValueError(f"unknown skill_id: {skill_id}")
    skill_plugin = plugins[skill_id]
    skill_loader = PluginSkillLoader(bus, skill_id)
    try:
        skill_loader.load(skill_plugin)
        wait_for_exit_signal()
    except KeyboardInterrupt:
        skill_loader.deactivate()
    except Exception:
        LOG.exception(f'Load of skill {skill_id} failed!')


def launch_standalone_skill(skill_directory, skill_id):
    """ run a skill standalone from a directory """
    bus = MessageBusClient()
    bus.run_in_thread()
    skill_loader = SkillLoader(bus, skill_directory,
                               skill_id=skill_id)
    try:
        skill_loader.load()
        wait_for_exit_signal()
    except KeyboardInterrupt:
        skill_loader.deactivate()
    except Exception:
        LOG.exception(f'Load of skill {skill_directory} failed!')


def _launch_script():
    """USAGE: ovos-skill-launcher {skill_id} [path/to/my/skill_id]"""
    if (args_count := len(sys.argv)) == 2:
        skill_id = sys.argv[1]

        # preference to local skills
        for p in get_skill_directories():
            if isdir(f"{p}/{skill_id}"):
                skill_directory = f"{p}/{skill_id}"
                LOG.info(f"found local skill, loading {skill_directory}")
                launch_standalone_skill(skill_directory, skill_id)
                break
        else:  # plugin skill
            LOG.info(f"found plugin skill {skill_id}")
            launch_plugin_skill(skill_id)

    elif args_count == 3:
        # user asked explicitly for a directory
        skill_id = sys.argv[1]
        skill_directory = sys.argv[2]
        launch_standalone_skill(skill_directory, skill_id)
    else:
        print("USAGE: ovos-skill-launcher {skill_id} [path/to/my/skill_id]")
        raise SystemExit(2)
