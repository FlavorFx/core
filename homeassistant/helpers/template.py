"""Template helper methods for rendering strings with Home Assistant data."""
from __future__ import annotations

from ast import literal_eval
import asyncio
import base64
import collections.abc
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timedelta
from functools import partial, wraps
import json
import logging
import math
from operator import attrgetter
import random
import re
import sys
from typing import Any, cast
from urllib.parse import urlencode as urllib_urlencode
import weakref

import jinja2
from jinja2 import pass_context
from jinja2.sandbox import ImmutableSandboxedEnvironment
from jinja2.utils import Namespace
import voluptuous as vol

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_UNIT_OF_MEASUREMENT,
    LENGTH_METERS,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    HomeAssistant,
    State,
    callback,
    split_entity_id,
    valid_entity_id,
)
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import (
    area_registry,
    device_registry,
    entity_registry,
    location as loc_helper,
)
from homeassistant.helpers.typing import TemplateVarsType
from homeassistant.loader import bind_hass
from homeassistant.util import convert, dt as dt_util, location as loc_util
from homeassistant.util.async_ import run_callback_threadsafe
from homeassistant.util.thread import ThreadWithException

# mypy: allow-untyped-defs, no-check-untyped-defs

_LOGGER = logging.getLogger(__name__)
_SENTINEL = object()
DATE_STR_FORMAT = "%Y-%m-%d %H:%M:%S"

_RENDER_INFO = "template.render_info"
_ENVIRONMENT = "template.environment"
_ENVIRONMENT_LIMITED = "template.environment_limited"
_ENVIRONMENT_STRICT = "template.environment_strict"

_RE_JINJA_DELIMITERS = re.compile(r"\{%|\{\{|\{#")
# Match "simple" ints and floats. -1.0, 1, +5, 5.0
_IS_NUMERIC = re.compile(r"^[+-]?(?!0\d)\d*(?:\.\d*)?$")

_RESERVED_NAMES = {"contextfunction", "evalcontextfunction", "environmentfunction"}

_GROUP_DOMAIN_PREFIX = "group."

_COLLECTABLE_STATE_ATTRIBUTES = {
    "state",
    "attributes",
    "last_changed",
    "last_updated",
    "context",
    "domain",
    "object_id",
    "name",
}

ALL_STATES_RATE_LIMIT = timedelta(minutes=1)
DOMAIN_STATES_RATE_LIMIT = timedelta(seconds=1)

template_cv: ContextVar[tuple[str, str] | None] = ContextVar(
    "template_cv", default=None
)


@bind_hass
def attach(hass: HomeAssistant, obj: Any) -> None:
    """Recursively attach hass to all template instances in list and dict."""
    if isinstance(obj, list):
        for child in obj:
            attach(hass, child)
    elif isinstance(obj, collections.abc.Mapping):
        for child_key, child_value in obj.items():
            attach(hass, child_key)
            attach(hass, child_value)
    elif isinstance(obj, Template):
        obj.hass = hass


def render_complex(
    value: Any, variables: TemplateVarsType = None, limited: bool = False
) -> Any:
    """Recursive template creator helper function."""
    if isinstance(value, list):
        return [render_complex(item, variables) for item in value]
    if isinstance(value, collections.abc.Mapping):
        return {
            render_complex(key, variables): render_complex(item, variables)
            for key, item in value.items()
        }
    if isinstance(value, Template):
        return value.async_render(variables, limited=limited)

    return value


def is_complex(value: Any) -> bool:
    """Test if data structure is a complex template."""
    if isinstance(value, Template):
        return True
    if isinstance(value, list):
        return any(is_complex(val) for val in value)
    if isinstance(value, collections.abc.Mapping):
        return any(is_complex(val) for val in value.keys()) or any(
            is_complex(val) for val in value.values()
        )
    return False


def is_template_string(maybe_template: str) -> bool:
    """Check if the input is a Jinja2 template."""
    return _RE_JINJA_DELIMITERS.search(maybe_template) is not None


class ResultWrapper:
    """Result wrapper class to store render result."""

    render_result: str | None


def gen_result_wrapper(kls):
    """Generate a result wrapper."""

    class Wrapper(kls, ResultWrapper):
        """Wrapper of a kls that can store render_result."""

        def __init__(self, *args: Any, render_result: str | None = None) -> None:
            super().__init__(*args)
            self.render_result = render_result

        def __str__(self) -> str:
            if self.render_result is None:
                # Can't get set repr to work
                if kls is set:
                    return str(set(self))

                return cast(str, kls.__str__(self))

            return self.render_result

    return Wrapper


class TupleWrapper(tuple, ResultWrapper):
    """Wrap a tuple."""

    # This is all magic to be allowed to subclass a tuple.

    def __new__(cls, value: tuple, *, render_result: str | None = None) -> TupleWrapper:
        """Create a new tuple class."""
        return super().__new__(cls, tuple(value))

    # pylint: disable=super-init-not-called

    def __init__(self, value: tuple, *, render_result: str | None = None) -> None:
        """Initialize a new tuple class."""
        self.render_result = render_result

    def __str__(self) -> str:
        """Return string representation."""
        if self.render_result is None:
            return super().__str__()

        return self.render_result


RESULT_WRAPPERS: dict[type, type] = {
    kls: gen_result_wrapper(kls)  # type: ignore[no-untyped-call]
    for kls in (list, dict, set)
}
RESULT_WRAPPERS[tuple] = TupleWrapper


def _true(arg: str) -> bool:
    return True


def _false(arg: str) -> bool:
    return False


class RenderInfo:
    """Holds information about a template render."""

    def __init__(self, template: Template) -> None:
        """Initialise."""
        self.template = template
        # Will be set sensibly once frozen.
        self.filter_lifecycle: Callable[[str], bool] = _true
        self.filter: Callable[[str], bool] = _true
        self._result: str | None = None
        self.is_static = False
        self.exception: TemplateError | None = None
        self.all_states = False
        self.all_states_lifecycle = False
        self.domains: collections.abc.Set[str] = set()
        self.domains_lifecycle: collections.abc.Set[str] = set()
        self.entities: collections.abc.Set[str] = set()
        self.rate_limit: timedelta | None = None
        self.has_time = False

    def __repr__(self) -> str:
        """Representation of RenderInfo."""
        return f"<RenderInfo {self.template} all_states={self.all_states} all_states_lifecycle={self.all_states_lifecycle} domains={self.domains} domains_lifecycle={self.domains_lifecycle} entities={self.entities} rate_limit={self.rate_limit}> has_time={self.has_time}"

    def _filter_domains_and_entities(self, entity_id: str) -> bool:
        """Template should re-render if the entity state changes when we match specific domains or entities."""
        return (
            split_entity_id(entity_id)[0] in self.domains or entity_id in self.entities
        )

    def _filter_entities(self, entity_id: str) -> bool:
        """Template should re-render if the entity state changes when we match specific entities."""
        return entity_id in self.entities

    def _filter_lifecycle_domains(self, entity_id: str) -> bool:
        """Template should re-render if the entity is added or removed with domains watched."""
        return split_entity_id(entity_id)[0] in self.domains_lifecycle

    def result(self) -> str:
        """Results of the template computation."""
        if self.exception is not None:
            raise self.exception
        return cast(str, self._result)

    def _freeze_static(self) -> None:
        self.is_static = True
        self._freeze_sets()
        self.all_states = False

    def _freeze_sets(self) -> None:
        self.entities = frozenset(self.entities)
        self.domains = frozenset(self.domains)
        self.domains_lifecycle = frozenset(self.domains_lifecycle)

    def _freeze(self) -> None:
        self._freeze_sets()

        if self.rate_limit is None:
            if self.all_states or self.exception:
                self.rate_limit = ALL_STATES_RATE_LIMIT
            elif self.domains or self.domains_lifecycle:
                self.rate_limit = DOMAIN_STATES_RATE_LIMIT

        if self.exception:
            return

        if not self.all_states_lifecycle:
            if self.domains_lifecycle:
                self.filter_lifecycle = self._filter_lifecycle_domains
            else:
                self.filter_lifecycle = _false

        if self.all_states:
            return

        if self.domains:
            self.filter = self._filter_domains_and_entities
        elif self.entities:
            self.filter = self._filter_entities
        else:
            self.filter = _false


class Template:
    """Class to hold a template and manage caching and rendering."""

    __slots__ = (
        "__weakref__",
        "template",
        "hass",
        "is_static",
        "_compiled_code",
        "_compiled",
        "_exc_info",
        "_limited",
        "_strict",
    )

    def __init__(self, template, hass=None):
        """Instantiate a template."""
        if not isinstance(template, str):
            raise TypeError("Expected template to be a string")

        self.template: str = template.strip()
        self._compiled_code = None
        self._compiled: jinja2.Template | None = None
        self.hass = hass
        self.is_static = not is_template_string(template)
        self._exc_info = None
        self._limited = None
        self._strict = None

    @property
    def _env(self) -> TemplateEnvironment:
        if self.hass is None:
            return _NO_HASS_ENV
        if self._limited:
            wanted_env = _ENVIRONMENT_LIMITED
        elif self._strict:
            wanted_env = _ENVIRONMENT_STRICT
        else:
            wanted_env = _ENVIRONMENT
        ret: TemplateEnvironment | None = self.hass.data.get(wanted_env)
        if ret is None:
            ret = self.hass.data[wanted_env] = TemplateEnvironment(self.hass, self._limited, self._strict)  # type: ignore[no-untyped-call]
        return ret

    def ensure_valid(self) -> None:
        """Return if template is valid."""
        with set_template(self.template, "compiling"):
            if self.is_static or self._compiled_code is not None:
                return

            try:
                self._compiled_code = self._env.compile(self.template)  # type: ignore[no-untyped-call]
            except jinja2.TemplateError as err:
                raise TemplateError(err) from err

    def render(
        self,
        variables: TemplateVarsType = None,
        parse_result: bool = True,
        limited: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Render given template.

        If limited is True, the template is not allowed to access any function or filter depending on hass or the state machine.
        """
        if self.is_static:
            if not parse_result or self.hass.config.legacy_templates:
                return self.template
            return self._parse_result(self.template)

        return run_callback_threadsafe(
            self.hass.loop,
            partial(self.async_render, variables, parse_result, limited, **kwargs),
        ).result()

    @callback
    def async_render(
        self,
        variables: TemplateVarsType = None,
        parse_result: bool = True,
        limited: bool = False,
        strict: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Render given template.

        This method must be run in the event loop.

        If limited is True, the template is not allowed to access any function or filter depending on hass or the state machine.
        """
        if self.is_static:
            if not parse_result or self.hass.config.legacy_templates:
                return self.template
            return self._parse_result(self.template)

        compiled = self._compiled or self._ensure_compiled(limited, strict)

        if variables is not None:
            kwargs.update(variables)

        try:
            render_result = _render_with_context(self.template, compiled, **kwargs)
        except Exception as err:
            raise TemplateError(err) from err

        render_result = render_result.strip()

        if self.hass.config.legacy_templates or not parse_result:
            return render_result

        return self._parse_result(render_result)

    def _parse_result(self, render_result: str) -> Any:  # pylint: disable=no-self-use
        """Parse the result."""
        try:
            result = literal_eval(render_result)

            if type(result) in RESULT_WRAPPERS:
                result = RESULT_WRAPPERS[type(result)](
                    result, render_result=render_result
                )

            # If the literal_eval result is a string, use the original
            # render, by not returning right here. The evaluation of strings
            # resulting in strings impacts quotes, to avoid unexpected
            # output; use the original render instead of the evaluated one.
            # Complex and scientific values are also unexpected. Filter them out.
            if (
                # Filter out string and complex numbers
                not isinstance(result, (str, complex))
                and (
                    # Pass if not numeric and not a boolean
                    not isinstance(result, (int, float))
                    # Or it's a boolean (inherit from int)
                    or isinstance(result, bool)
                    # Or if it's a digit
                    or _IS_NUMERIC.match(render_result) is not None
                )
            ):
                return result
        except (ValueError, TypeError, SyntaxError, MemoryError):
            pass

        return render_result

    async def async_render_will_timeout(
        self,
        timeout: float,
        variables: TemplateVarsType = None,
        strict: bool = False,
        **kwargs: Any,
    ) -> bool:
        """Check to see if rendering a template will timeout during render.

        This is intended to check for expensive templates
        that will make the system unstable.  The template
        is rendered in the executor to ensure it does not
        tie up the event loop.

        This function is not a security control and is only
        intended to be used as a safety check when testing
        templates.

        This method must be run in the event loop.
        """
        if self.is_static:
            return False

        compiled = self._compiled or self._ensure_compiled(strict=strict)

        if variables is not None:
            kwargs.update(variables)

        self._exc_info = None
        finish_event = asyncio.Event()

        def _render_template() -> None:
            try:
                _render_with_context(self.template, compiled, **kwargs)
            except TimeoutError:
                pass
            except Exception:  # pylint: disable=broad-except
                self._exc_info = sys.exc_info()
            finally:
                run_callback_threadsafe(self.hass.loop, finish_event.set)

        try:
            template_render_thread = ThreadWithException(target=_render_template)
            template_render_thread.start()
            await asyncio.wait_for(finish_event.wait(), timeout=timeout)
            if self._exc_info:
                raise TemplateError(self._exc_info[1].with_traceback(self._exc_info[2]))
        except asyncio.TimeoutError:
            template_render_thread.raise_exc(TimeoutError)
            return True
        finally:
            template_render_thread.join()

        return False

    @callback
    def async_render_to_info(
        self, variables: TemplateVarsType = None, strict: bool = False, **kwargs: Any
    ) -> RenderInfo:
        """Render the template and collect an entity filter."""
        assert self.hass and _RENDER_INFO not in self.hass.data

        render_info = RenderInfo(self)

        # pylint: disable=protected-access
        if self.is_static:
            render_info._result = self.template.strip()
            render_info._freeze_static()
            return render_info

        self.hass.data[_RENDER_INFO] = render_info
        try:
            render_info._result = self.async_render(variables, strict=strict, **kwargs)
        except TemplateError as ex:
            render_info.exception = ex
        finally:
            del self.hass.data[_RENDER_INFO]

        render_info._freeze()
        return render_info

    def render_with_possible_json_value(self, value, error_value=_SENTINEL):
        """Render template with value exposed.

        If valid JSON will expose value_json too.
        """
        if self.is_static:
            return self.template

        return run_callback_threadsafe(
            self.hass.loop,
            self.async_render_with_possible_json_value,
            value,
            error_value,
        ).result()

    @callback
    def async_render_with_possible_json_value(
        self, value, error_value=_SENTINEL, variables=None
    ):
        """Render template with value exposed.

        If valid JSON will expose value_json too.

        This method must be run in the event loop.
        """
        if self.is_static:
            return self.template

        if self._compiled is None:
            self._ensure_compiled()

        variables = dict(variables or {})
        variables["value"] = value

        with suppress(ValueError, TypeError):
            variables["value_json"] = json.loads(value)

        try:
            return _render_with_context(
                self.template, self._compiled, **variables
            ).strip()
        except jinja2.TemplateError as ex:
            if error_value is _SENTINEL:
                _LOGGER.error(
                    "Error parsing value: %s (value: %s, template: %s)",
                    ex,
                    value,
                    self.template,
                )
            return value if error_value is _SENTINEL else error_value

    def _ensure_compiled(
        self, limited: bool = False, strict: bool = False
    ) -> jinja2.Template:
        """Bind a template to a specific hass instance."""
        self.ensure_valid()

        assert self.hass is not None, "hass variable not set on template"
        assert (
            self._limited is None or self._limited == limited
        ), "can't change between limited and non limited template"
        assert (
            self._strict is None or self._strict == strict
        ), "can't change between strict and non strict template"
        assert not (strict and limited), "can't combine strict and limited template"

        self._limited = limited
        self._strict = strict
        env = self._env

        self._compiled = jinja2.Template.from_code(
            env, self._compiled_code, env.globals, None
        )

        return self._compiled

    def __eq__(self, other):
        """Compare template with another."""
        return (
            self.__class__ == other.__class__
            and self.template == other.template
            and self.hass == other.hass
        )

    def __hash__(self) -> int:
        """Hash code for template."""
        return hash(self.template)

    def __repr__(self) -> str:
        """Representation of Template."""
        return 'Template("' + self.template + '")'


class AllStates:
    """Class to expose all HA states as attributes."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize all states."""
        self._hass = hass

    def __getattr__(self, name):
        """Return the domain state."""
        if "." in name:
            return _get_state_if_valid(self._hass, name)

        if name in _RESERVED_NAMES:
            return None

        if not valid_entity_id(f"{name}.entity"):
            raise TemplateError(f"Invalid domain name '{name}'")

        return DomainStates(self._hass, name)

    # Jinja will try __getitem__ first and it avoids the need
    # to call is_safe_attribute
    __getitem__ = __getattr__

    def _collect_all(self) -> None:
        render_info = self._hass.data.get(_RENDER_INFO)
        if render_info is not None:
            render_info.all_states = True

    def _collect_all_lifecycle(self) -> None:
        render_info = self._hass.data.get(_RENDER_INFO)
        if render_info is not None:
            render_info.all_states_lifecycle = True

    def __iter__(self):
        """Return all states."""
        self._collect_all()
        return _state_generator(self._hass, None)

    def __len__(self) -> int:
        """Return number of states."""
        self._collect_all_lifecycle()
        return self._hass.states.async_entity_ids_count()

    def __call__(self, entity_id):
        """Return the states."""
        state = _get_state(self._hass, entity_id)
        return STATE_UNKNOWN if state is None else state.state

    def __repr__(self) -> str:
        """Representation of All States."""
        return "<template AllStates>"


class DomainStates:
    """Class to expose a specific HA domain as attributes."""

    def __init__(self, hass: HomeAssistant, domain: str) -> None:
        """Initialize the domain states."""
        self._hass = hass
        self._domain = domain

    def __getattr__(self, name):
        """Return the states."""
        return _get_state_if_valid(self._hass, f"{self._domain}.{name}")

    # Jinja will try __getitem__ first and it avoids the need
    # to call is_safe_attribute
    __getitem__ = __getattr__

    def _collect_domain(self) -> None:
        entity_collect = self._hass.data.get(_RENDER_INFO)
        if entity_collect is not None:
            entity_collect.domains.add(self._domain)

    def _collect_domain_lifecycle(self) -> None:
        entity_collect = self._hass.data.get(_RENDER_INFO)
        if entity_collect is not None:
            entity_collect.domains_lifecycle.add(self._domain)

    def __iter__(self):
        """Return the iteration over all the states."""
        self._collect_domain()
        return _state_generator(self._hass, self._domain)

    def __len__(self) -> int:
        """Return number of states."""
        self._collect_domain_lifecycle()
        return self._hass.states.async_entity_ids_count(self._domain)

    def __repr__(self) -> str:
        """Representation of Domain States."""
        return f"<template DomainStates('{self._domain}')>"


class TemplateState(State):
    """Class to represent a state object in a template."""

    __slots__ = ("_hass", "_state", "_collect")

    # Inheritance is done so functions that check against State keep working
    # pylint: disable=super-init-not-called
    def __init__(self, hass: HomeAssistant, state: State, collect: bool = True) -> None:
        """Initialize template state."""
        self._hass = hass
        self._state = state
        self._collect = collect

    def _collect_state(self) -> None:
        if self._collect and _RENDER_INFO in self._hass.data:
            self._hass.data[_RENDER_INFO].entities.add(self._state.entity_id)

    # Jinja will try __getitem__ first and it avoids the need
    # to call is_safe_attribute
    def __getitem__(self, item):
        """Return a property as an attribute for jinja."""
        if item in _COLLECTABLE_STATE_ATTRIBUTES:
            # _collect_state inlined here for performance
            if self._collect and _RENDER_INFO in self._hass.data:
                self._hass.data[_RENDER_INFO].entities.add(self._state.entity_id)
            return getattr(self._state, item)
        if item == "entity_id":
            return self._state.entity_id
        if item == "state_with_unit":
            return self.state_with_unit
        raise KeyError

    @property
    def entity_id(self):
        """Wrap State.entity_id.

        Intentionally does not collect state
        """
        return self._state.entity_id

    @property
    def state(self):
        """Wrap State.state."""
        self._collect_state()
        return self._state.state

    @property
    def attributes(self):
        """Wrap State.attributes."""
        self._collect_state()
        return self._state.attributes

    @property
    def last_changed(self):
        """Wrap State.last_changed."""
        self._collect_state()
        return self._state.last_changed

    @property
    def last_updated(self):
        """Wrap State.last_updated."""
        self._collect_state()
        return self._state.last_updated

    @property
    def context(self):
        """Wrap State.context."""
        self._collect_state()
        return self._state.context

    @property
    def domain(self):
        """Wrap State.domain."""
        self._collect_state()
        return self._state.domain

    @property
    def object_id(self):
        """Wrap State.object_id."""
        self._collect_state()
        return self._state.object_id

    @property
    def name(self):
        """Wrap State.name."""
        self._collect_state()
        return self._state.name

    @property
    def state_with_unit(self) -> str:
        """Return the state concatenated with the unit if available."""
        self._collect_state()
        unit = self._state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        return f"{self._state.state} {unit}" if unit else self._state.state

    def __eq__(self, other: Any) -> bool:
        """Ensure we collect on equality check."""
        self._collect_state()
        return self._state.__eq__(other)

    def __repr__(self) -> str:
        """Representation of Template State."""
        return f"<template TemplateState({self._state.__repr__()})>"


def _collect_state(hass: HomeAssistant, entity_id: str) -> None:
    entity_collect = hass.data.get(_RENDER_INFO)
    if entity_collect is not None:
        entity_collect.entities.add(entity_id)


def _state_generator(hass: HomeAssistant, domain: str | None) -> Generator:
    """State generator for a domain or all states."""
    for state in sorted(hass.states.async_all(domain), key=attrgetter("entity_id")):
        yield TemplateState(hass, state, collect=False)


def _get_state_if_valid(hass: HomeAssistant, entity_id: str) -> TemplateState | None:
    state = hass.states.get(entity_id)
    if state is None and not valid_entity_id(entity_id):
        raise TemplateError(f"Invalid entity ID '{entity_id}'")  # type: ignore
    return _get_template_state_from_state(hass, entity_id, state)


def _get_state(hass: HomeAssistant, entity_id: str) -> TemplateState | None:
    return _get_template_state_from_state(hass, entity_id, hass.states.get(entity_id))


def _get_template_state_from_state(
    hass: HomeAssistant, entity_id: str, state: State | None
) -> TemplateState | None:
    if state is None:
        # Only need to collect if none, if not none collect first actual
        # access to the state properties in the state wrapper.
        _collect_state(hass, entity_id)
        return None
    return TemplateState(hass, state)


def _resolve_state(
    hass: HomeAssistant, entity_id_or_state: Any
) -> State | TemplateState | None:
    """Return state or entity_id if given."""
    if isinstance(entity_id_or_state, State):
        return entity_id_or_state
    if isinstance(entity_id_or_state, str):
        return _get_state(hass, entity_id_or_state)
    return None


def result_as_boolean(template_result: str | None) -> bool:
    """Convert the template result to a boolean.

    True/not 0/'1'/'true'/'yes'/'on'/'enable' are considered truthy
    False/0/None/'0'/'false'/'no'/'off'/'disable' are considered falsy

    """
    if template_result is None:
        return False

    try:
        # Import here, not at top-level to avoid circular import
        from homeassistant.helpers import (  # pylint: disable=import-outside-toplevel
            config_validation as cv,
        )

        return cv.boolean(template_result)
    except vol.Invalid:
        return False


def expand(hass: HomeAssistant, *args: Any) -> Iterable[State]:
    """Expand out any groups into entity states."""
    search = list(args)
    found = {}
    while search:
        entity = search.pop()
        if isinstance(entity, str):
            entity_id = entity
            entity = _get_state(hass, entity)
            if entity is None:
                continue
        elif isinstance(entity, State):
            entity_id = entity.entity_id
        elif isinstance(entity, collections.abc.Iterable):
            search += entity
            continue
        else:
            # ignore other types
            continue

        if entity_id.startswith(_GROUP_DOMAIN_PREFIX):
            # Collect state will be called in here since it's wrapped
            group_entities = entity.attributes.get(ATTR_ENTITY_ID)
            if group_entities:
                search += group_entities
        else:
            _collect_state(hass, entity_id)
            found[entity_id] = entity

    return sorted(found.values(), key=lambda a: a.entity_id)


def device_entities(hass: HomeAssistant, _device_id: str) -> Iterable[str]:
    """Get entity ids for entities tied to a device."""
    entity_reg = entity_registry.async_get(hass)
    entries = entity_registry.async_entries_for_device(entity_reg, _device_id)
    return [entry.entity_id for entry in entries]


def device_id(hass: HomeAssistant, entity_id_or_device_name: str) -> str | None:
    """Get a device ID from an entity ID or device name."""
    entity_reg = entity_registry.async_get(hass)
    entity = entity_reg.async_get(entity_id_or_device_name)
    if entity is not None:
        return entity.device_id

    dev_reg = device_registry.async_get(hass)
    return next(
        (
            id
            for id, device in dev_reg.devices.items()
            if (name := device.name_by_user or device.name)
            and (str(entity_id_or_device_name) == name)
        ),
        None,
    )


def device_attr(hass: HomeAssistant, device_or_entity_id: str, attr_name: str) -> Any:
    """Get the device specific attribute."""
    device_reg = device_registry.async_get(hass)
    if not isinstance(device_or_entity_id, str):
        raise TemplateError("Must provide a device or entity ID")
    device = None
    if (
        "." in device_or_entity_id
        and (_device_id := device_id(hass, device_or_entity_id)) is not None
    ):
        device = device_reg.async_get(_device_id)
    elif "." not in device_or_entity_id:
        device = device_reg.async_get(device_or_entity_id)
    if device is None or not hasattr(device, attr_name):
        return None
    return getattr(device, attr_name)


def is_device_attr(
    hass: HomeAssistant, device_or_entity_id: str, attr_name: str, attr_value: Any
) -> bool:
    """Test if a device's attribute is a specific value."""
    return bool(device_attr(hass, device_or_entity_id, attr_name) == attr_value)


def area_id(hass: HomeAssistant, lookup_value: str) -> str | None:
    """Get the area ID from an area name, device id, or entity id."""
    area_reg = area_registry.async_get(hass)
    if area := area_reg.async_get_area_by_name(str(lookup_value)):
        return area.id

    ent_reg = entity_registry.async_get(hass)
    dev_reg = device_registry.async_get(hass)
    # Import here, not at top-level to avoid circular import
    from homeassistant.helpers import (  # pylint: disable=import-outside-toplevel
        config_validation as cv,
    )

    try:
        cv.entity_id(lookup_value)
    except vol.Invalid:
        pass
    else:
        if entity := ent_reg.async_get(lookup_value):
            # If entity has an area ID, return that
            if entity.area_id:
                return entity.area_id
            # If entity has a device ID, return the area ID for the device
            if entity.device_id and (device := dev_reg.async_get(entity.device_id)):
                return device.area_id

    # Check if this could be a device ID
    if device := dev_reg.async_get(lookup_value):
        return device.area_id

    return None


def _get_area_name(area_reg: area_registry.AreaRegistry, valid_area_id: str) -> str:
    """Get area name from valid area ID."""
    area = area_reg.async_get_area(valid_area_id)
    assert area
    return area.name


def area_name(hass: HomeAssistant, lookup_value: str) -> str | None:
    """Get the area name from an area id, device id, or entity id."""
    area_reg = area_registry.async_get(hass)
    area = area_reg.async_get_area(lookup_value)
    if area:
        return area.name

    dev_reg = device_registry.async_get(hass)
    ent_reg = entity_registry.async_get(hass)
    # Import here, not at top-level to avoid circular import
    from homeassistant.helpers import (  # pylint: disable=import-outside-toplevel
        config_validation as cv,
    )

    try:
        cv.entity_id(lookup_value)
    except vol.Invalid:
        pass
    else:
        if entity := ent_reg.async_get(lookup_value):
            # If entity has an area ID, get the area name for that
            if entity.area_id:
                return _get_area_name(area_reg, entity.area_id)
            # If entity has a device ID and the device exists with an area ID, get the
            # area name for that
            if (
                entity.device_id
                and (device := dev_reg.async_get(entity.device_id))
                and device.area_id
            ):
                return _get_area_name(area_reg, device.area_id)

    if (device := dev_reg.async_get(lookup_value)) and device.area_id:
        return _get_area_name(area_reg, device.area_id)

    return None


def closest(hass, *args):
    """Find closest entity.

    Closest to home:
        closest(states)
        closest(states.device_tracker)
        closest('group.children')
        closest(states.group.children)

    Closest to a point:
        closest(23.456, 23.456, 'group.children')
        closest('zone.school', 'group.children')
        closest(states.zone.school, 'group.children')

    As a filter:
        states | closest
        states.device_tracker | closest
        ['group.children', states.device_tracker] | closest
        'group.children' | closest(23.456, 23.456)
        states.device_tracker | closest('zone.school')
        'group.children' | closest(states.zone.school)

    """
    if len(args) == 1:
        latitude = hass.config.latitude
        longitude = hass.config.longitude
        entities = args[0]

    elif len(args) == 2:
        point_state = _resolve_state(hass, args[0])

        if point_state is None:
            _LOGGER.warning("Closest:Unable to find state %s", args[0])
            return None
        if not loc_helper.has_location(point_state):
            _LOGGER.warning(
                "Closest:State does not contain valid location: %s", point_state
            )
            return None

        latitude = point_state.attributes.get(ATTR_LATITUDE)
        longitude = point_state.attributes.get(ATTR_LONGITUDE)

        entities = args[1]

    else:
        latitude = convert(args[0], float)
        longitude = convert(args[1], float)

        if latitude is None or longitude is None:
            _LOGGER.warning(
                "Closest:Received invalid coordinates: %s, %s", args[0], args[1]
            )
            return None

        entities = args[2]

    states = expand(hass, entities)

    # state will already be wrapped here
    return loc_helper.closest(latitude, longitude, states)


def closest_filter(hass, *args):
    """Call closest as a filter. Need to reorder arguments."""
    new_args = list(args[1:])
    new_args.append(args[0])
    return closest(hass, *new_args)


def distance(hass, *args):
    """Calculate distance.

    Will calculate distance from home to a point or between points.
    Points can be passed in using state objects or lat/lng coordinates.
    """
    locations = []

    to_process = list(args)

    while to_process:
        value = to_process.pop(0)
        if isinstance(value, str) and not valid_entity_id(value):
            point_state = None
        else:
            point_state = _resolve_state(hass, value)

        if point_state is None:
            # We expect this and next value to be lat&lng
            if not to_process:
                _LOGGER.warning(
                    "Distance:Expected latitude and longitude, got %s", value
                )
                return None

            value_2 = to_process.pop(0)
            latitude = convert(value, float)
            longitude = convert(value_2, float)

            if latitude is None or longitude is None:
                _LOGGER.warning(
                    "Distance:Unable to process latitude and longitude: %s, %s",
                    value,
                    value_2,
                )
                return None

        else:
            if not loc_helper.has_location(point_state):
                _LOGGER.warning(
                    "Distance:State does not contain valid location: %s", point_state
                )
                return None

            latitude = point_state.attributes.get(ATTR_LATITUDE)
            longitude = point_state.attributes.get(ATTR_LONGITUDE)

        locations.append((latitude, longitude))

    if len(locations) == 1:
        return hass.config.distance(*locations[0])

    return hass.config.units.length(
        loc_util.distance(*locations[0] + locations[1]), LENGTH_METERS
    )


def is_state(hass: HomeAssistant, entity_id: str, state: State) -> bool:
    """Test if a state is a specific value."""
    state_obj = _get_state(hass, entity_id)
    return state_obj is not None and state_obj.state == state


def is_state_attr(hass: HomeAssistant, entity_id: str, name: str, value: Any) -> bool:
    """Test if a state's attribute is a specific value."""
    attr = state_attr(hass, entity_id, name)
    return attr is not None and attr == value


def state_attr(hass: HomeAssistant, entity_id: str, name: str) -> Any:
    """Get a specific attribute from a state."""
    state_obj = _get_state(hass, entity_id)
    if state_obj is not None:
        return state_obj.attributes.get(name)
    return None


def now(hass: HomeAssistant) -> datetime:
    """Record fetching now."""
    render_info = hass.data.get(_RENDER_INFO)
    if render_info is not None:
        render_info.has_time = True

    return dt_util.now()


def utcnow(hass: HomeAssistant) -> datetime:
    """Record fetching utcnow."""
    render_info = hass.data.get(_RENDER_INFO)
    if render_info is not None:
        render_info.has_time = True

    return dt_util.utcnow()


def warn_no_default(function, value, default):
    """Log warning if no default is specified."""
    template, action = template_cv.get() or ("", "rendering or compiling")
    _LOGGER.warning(
        (
            "Template warning: '%s' got invalid input '%s' when %s template '%s' "
            "but no default was specified. Currently '%s' will return '%s', however this template will fail "
            "to render in Home Assistant core 2022.1"
        ),
        function,
        value,
        action,
        template,
        function,
        default,
    )


def forgiving_round(value, precision=0, method="common", default=_SENTINEL):
    """Filter to round a value."""
    try:
        # support rounding methods like jinja
        multiplier = float(10 ** precision)
        if method == "ceil":
            value = math.ceil(float(value) * multiplier) / multiplier
        elif method == "floor":
            value = math.floor(float(value) * multiplier) / multiplier
        elif method == "half":
            value = round(float(value) * 2) / 2
        else:
            # if method is common or something else, use common rounding
            value = round(float(value), precision)
        return int(value) if precision == 0 else value
    except (ValueError, TypeError):
        # If value can't be converted to float
        if default is _SENTINEL:
            warn_no_default("round", value, value)
            return value
        return default


def multiply(value, amount, default=_SENTINEL):
    """Filter to convert value to float and multiply it."""
    try:
        return float(value) * amount
    except (ValueError, TypeError):
        # If value can't be converted to float
        if default is _SENTINEL:
            warn_no_default("multiply", value, value)
            return value
        return default


def logarithm(value, base=math.e, default=_SENTINEL):
    """Filter and function to get logarithm of the value with a specific base."""
    try:
        return math.log(float(value), float(base))
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("log", value, value)
            return value
        return default


def sine(value, default=_SENTINEL):
    """Filter and function to get sine of the value."""
    try:
        return math.sin(float(value))
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("sin", value, value)
            return value
        return default


def cosine(value, default=_SENTINEL):
    """Filter and function to get cosine of the value."""
    try:
        return math.cos(float(value))
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("cos", value, value)
            return value
        return default


def tangent(value, default=_SENTINEL):
    """Filter and function to get tangent of the value."""
    try:
        return math.tan(float(value))
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("tan", value, value)
            return value
        return default


def arc_sine(value, default=_SENTINEL):
    """Filter and function to get arc sine of the value."""
    try:
        return math.asin(float(value))
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("asin", value, value)
            return value
        return default


def arc_cosine(value, default=_SENTINEL):
    """Filter and function to get arc cosine of the value."""
    try:
        return math.acos(float(value))
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("acos", value, value)
            return value
        return default


def arc_tangent(value, default=_SENTINEL):
    """Filter and function to get arc tangent of the value."""
    try:
        return math.atan(float(value))
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("atan", value, value)
            return value
        return default


def arc_tangent2(*args, default=_SENTINEL):
    """Filter and function to calculate four quadrant arc tangent of y / x.

    The parameters to atan2 may be passed either in an iterable or as separate arguments
    The default value may be passed either as a positional or in a keyword argument
    """
    try:
        if 1 <= len(args) <= 2 and isinstance(args[0], (list, tuple)):
            if len(args) == 2 and default is _SENTINEL:
                # Default value passed as a positional argument
                default = args[1]
            args = args[0]
        elif len(args) == 3 and default is _SENTINEL:
            # Default value passed as a positional argument
            default = args[2]

        return math.atan2(float(args[0]), float(args[1]))
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("atan2", args, args)
            return args
        return default


def square_root(value, default=_SENTINEL):
    """Filter and function to get square root of the value."""
    try:
        return math.sqrt(float(value))
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("sqrt", value, value)
            return value
        return default


def timestamp_custom(value, date_format=DATE_STR_FORMAT, local=True, default=_SENTINEL):
    """Filter to convert given timestamp to format."""
    try:
        date = dt_util.utc_from_timestamp(value)

        if local:
            date = dt_util.as_local(date)

        return date.strftime(date_format)
    except (ValueError, TypeError):
        # If timestamp can't be converted
        if default is _SENTINEL:
            warn_no_default("timestamp_custom", value, value)
            return value
        return default


def timestamp_local(value, default=_SENTINEL):
    """Filter to convert given timestamp to local date/time."""
    try:
        return dt_util.as_local(dt_util.utc_from_timestamp(value)).strftime(
            DATE_STR_FORMAT
        )
    except (ValueError, TypeError):
        # If timestamp can't be converted
        if default is _SENTINEL:
            warn_no_default("timestamp_local", value, value)
            return value
        return default


def timestamp_utc(value, default=_SENTINEL):
    """Filter to convert given timestamp to UTC date/time."""
    try:
        return dt_util.utc_from_timestamp(value).strftime(DATE_STR_FORMAT)
    except (ValueError, TypeError):
        # If timestamp can't be converted
        if default is _SENTINEL:
            warn_no_default("timestamp_utc", value, value)
            return value
        return default


def forgiving_as_timestamp(value, default=_SENTINEL):
    """Filter and function which tries to convert value to timestamp."""
    try:
        return dt_util.as_timestamp(value)
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("as_timestamp", value, None)
            return None
        return default


def strptime(string, fmt, default=_SENTINEL):
    """Parse a time string to datetime."""
    try:
        return datetime.strptime(string, fmt)
    except (ValueError, AttributeError, TypeError):
        if default is _SENTINEL:
            warn_no_default("strptime", string, string)
            return string
        return default


def fail_when_undefined(value):
    """Filter to force a failure when the value is undefined."""
    if isinstance(value, jinja2.Undefined):
        value()
    return value


def forgiving_float(value, default=_SENTINEL):
    """Try to convert value to a float."""
    try:
        return float(value)
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("float", value, value)
            return value
        return default


def forgiving_float_filter(value, default=_SENTINEL):
    """Try to convert value to a float."""
    try:
        return float(value)
    except (ValueError, TypeError):
        if default is _SENTINEL:
            warn_no_default("float", value, 0)
            return 0
        return default


def forgiving_int(value, default=_SENTINEL, base=10):
    """Try to convert value to an int, and warn if it fails."""
    result = jinja2.filters.do_int(value, default=default, base=base)
    if result is _SENTINEL:
        warn_no_default("int", value, value)
        return value
    return result


def forgiving_int_filter(value, default=_SENTINEL, base=10):
    """Try to convert value to an int, and warn if it fails."""
    result = jinja2.filters.do_int(value, default=default, base=base)
    if result is _SENTINEL:
        warn_no_default("int", value, 0)
        return 0
    return result


def is_number(value):
    """Try to convert value to a float."""
    try:
        fvalue = float(value)
    except (ValueError, TypeError):
        return False
    if math.isnan(fvalue) or math.isinf(fvalue):
        return False
    return True


def regex_match(value, find="", ignorecase=False):
    """Match value using regex."""
    if not isinstance(value, str):
        value = str(value)
    flags = re.I if ignorecase else 0
    return bool(re.match(find, value, flags))


def regex_replace(value="", find="", replace="", ignorecase=False):
    """Replace using regex."""
    if not isinstance(value, str):
        value = str(value)
    flags = re.I if ignorecase else 0
    regex = re.compile(find, flags)
    return regex.sub(replace, value)


def regex_search(value, find="", ignorecase=False):
    """Search using regex."""
    if not isinstance(value, str):
        value = str(value)
    flags = re.I if ignorecase else 0
    return bool(re.search(find, value, flags))


def regex_findall_index(value, find="", index=0, ignorecase=False):
    """Find all matches using regex and then pick specific match index."""
    return regex_findall(value, find, ignorecase)[index]


def regex_findall(value, find="", ignorecase=False):
    """Find all matches using regex."""
    if not isinstance(value, str):
        value = str(value)
    flags = re.I if ignorecase else 0
    return re.findall(find, value, flags)


def bitwise_and(first_value, second_value):
    """Perform a bitwise and operation."""
    return first_value & second_value


def bitwise_or(first_value, second_value):
    """Perform a bitwise or operation."""
    return first_value | second_value


def base64_encode(value):
    """Perform base64 encode."""
    return base64.b64encode(value.encode("utf-8")).decode("utf-8")


def base64_decode(value):
    """Perform base64 denode."""
    return base64.b64decode(value).decode("utf-8")


def ordinal(value):
    """Perform ordinal conversion."""
    return str(value) + (
        list(["th", "st", "nd", "rd"] + ["th"] * 6)[(int(str(value)[-1])) % 10]
        if int(str(value)[-2:]) % 100 not in range(11, 14)
        else "th"
    )


def from_json(value):
    """Convert a JSON string to an object."""
    return json.loads(value)


def to_json(value):
    """Convert an object to a JSON string."""
    return json.dumps(value)


@pass_context
def random_every_time(context, values):
    """Choose a random value.

    Unlike Jinja's random filter,
    this is context-dependent to avoid caching the chosen value.
    """
    return random.choice(values)


def relative_time(value):
    """
    Take a datetime and return its "age" as a string.

    The age can be in second, minute, hour, day, month or year. Only the
    biggest unit is considered, e.g. if it's 2 days and 3 hours, "2 days" will
    be returned.
    Make sure date is not in the future, or else it will return None.

    If the input are not a datetime object the input will be returned unmodified.
    """
    if not isinstance(value, datetime):
        return value
    if not value.tzinfo:
        value = dt_util.as_local(value)
    if dt_util.now() < value:
        return value
    return dt_util.get_age(value)


def urlencode(value):
    """Urlencode dictionary and return as UTF-8 string."""
    return urllib_urlencode(value).encode("utf-8")


@contextmanager
def set_template(template_str: str, action: str) -> Generator:
    """Store template being parsed or rendered in a Contextvar to aid error handling."""
    template_cv.set((template_str, action))
    try:
        yield
    finally:
        template_cv.set(None)


def _render_with_context(
    template_str: str, template: jinja2.Template, **kwargs: Any
) -> str:
    """Store template being rendered in a ContextVar to aid error handling."""
    with set_template(template_str, "rendering"):
        return template.render(**kwargs)


class LoggingUndefined(jinja2.Undefined):
    """Log on undefined variables."""

    def _log_message(self):
        template, action = template_cv.get() or ("", "rendering or compiling")
        _LOGGER.warning(
            "Template variable warning: %s when %s '%s'",
            self._undefined_message,
            action,
            template,
        )

    def _fail_with_undefined_error(self, *args, **kwargs):
        try:
            return super()._fail_with_undefined_error(*args, **kwargs)
        except self._undefined_exception as ex:
            template, action = template_cv.get() or ("", "rendering or compiling")
            _LOGGER.error(
                "Template variable error: %s when %s '%s'",
                self._undefined_message,
                action,
                template,
            )
            raise ex

    def __str__(self):
        """Log undefined __str___."""
        self._log_message()
        return super().__str__()

    def __iter__(self):
        """Log undefined __iter___."""
        self._log_message()
        return super().__iter__()

    def __bool__(self):
        """Log undefined __bool___."""
        self._log_message()
        return super().__bool__()


class TemplateEnvironment(ImmutableSandboxedEnvironment):
    """The Home Assistant template environment."""

    def __init__(self, hass, limited=False, strict=False):
        """Initialise template environment."""
        if not strict:
            undefined = LoggingUndefined
        else:
            undefined = jinja2.StrictUndefined
        super().__init__(undefined=undefined)
        self.hass = hass
        self.template_cache = weakref.WeakValueDictionary()
        self.filters["round"] = forgiving_round
        self.filters["multiply"] = multiply
        self.filters["log"] = logarithm
        self.filters["sin"] = sine
        self.filters["cos"] = cosine
        self.filters["tan"] = tangent
        self.filters["asin"] = arc_sine
        self.filters["acos"] = arc_cosine
        self.filters["atan"] = arc_tangent
        self.filters["atan2"] = arc_tangent2
        self.filters["sqrt"] = square_root
        self.filters["as_datetime"] = dt_util.parse_datetime
        self.filters["as_timestamp"] = forgiving_as_timestamp
        self.filters["as_local"] = dt_util.as_local
        self.filters["timestamp_custom"] = timestamp_custom
        self.filters["timestamp_local"] = timestamp_local
        self.filters["timestamp_utc"] = timestamp_utc
        self.filters["to_json"] = to_json
        self.filters["from_json"] = from_json
        self.filters["is_defined"] = fail_when_undefined
        self.filters["max"] = max
        self.filters["min"] = min
        self.filters["random"] = random_every_time
        self.filters["base64_encode"] = base64_encode
        self.filters["base64_decode"] = base64_decode
        self.filters["ordinal"] = ordinal
        self.filters["regex_match"] = regex_match
        self.filters["regex_replace"] = regex_replace
        self.filters["regex_search"] = regex_search
        self.filters["regex_findall"] = regex_findall
        self.filters["regex_findall_index"] = regex_findall_index
        self.filters["bitwise_and"] = bitwise_and
        self.filters["bitwise_or"] = bitwise_or
        self.filters["ord"] = ord
        self.filters["is_number"] = is_number
        self.filters["float"] = forgiving_float_filter
        self.filters["int"] = forgiving_int_filter
        self.globals["log"] = logarithm
        self.globals["sin"] = sine
        self.globals["cos"] = cosine
        self.globals["tan"] = tangent
        self.globals["sqrt"] = square_root
        self.globals["pi"] = math.pi
        self.globals["tau"] = math.pi * 2
        self.globals["e"] = math.e
        self.globals["asin"] = arc_sine
        self.globals["acos"] = arc_cosine
        self.globals["atan"] = arc_tangent
        self.globals["atan2"] = arc_tangent2
        self.globals["float"] = forgiving_float
        self.globals["as_datetime"] = dt_util.parse_datetime
        self.globals["as_local"] = dt_util.as_local
        self.globals["as_timestamp"] = forgiving_as_timestamp
        self.globals["relative_time"] = relative_time
        self.globals["timedelta"] = timedelta
        self.globals["strptime"] = strptime
        self.globals["urlencode"] = urlencode
        self.globals["max"] = max
        self.globals["min"] = min
        self.globals["is_number"] = is_number
        self.globals["int"] = forgiving_int
        self.tests["match"] = regex_match
        self.tests["search"] = regex_search

        if hass is None:
            return

        # We mark these as a context functions to ensure they get
        # evaluated fresh with every execution, rather than executed
        # at compile time and the value stored. The context itself
        # can be discarded, we only need to get at the hass object.
        def hassfunction(func):
            """Wrap function that depend on hass."""

            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(hass, *args[1:], **kwargs)

            return pass_context(wrapper)

        self.globals["device_entities"] = hassfunction(device_entities)
        self.filters["device_entities"] = pass_context(self.globals["device_entities"])

        self.globals["device_attr"] = hassfunction(device_attr)
        self.globals["is_device_attr"] = hassfunction(is_device_attr)

        self.globals["device_id"] = hassfunction(device_id)
        self.filters["device_id"] = pass_context(self.globals["device_id"])

        self.globals["area_id"] = hassfunction(area_id)
        self.filters["area_id"] = pass_context(self.globals["area_id"])

        self.globals["area_name"] = hassfunction(area_name)
        self.filters["area_name"] = pass_context(self.globals["area_name"])

        if limited:
            # Only device_entities is available to limited templates, mark other
            # functions and filters as unsupported.
            def unsupported(name):
                def warn_unsupported(*args, **kwargs):
                    raise TemplateError(
                        f"Use of '{name}' is not supported in limited templates"
                    )

                return warn_unsupported

            hass_globals = [
                "closest",
                "distance",
                "expand",
                "is_state",
                "is_state_attr",
                "state_attr",
                "states",
                "utcnow",
                "now",
                "device_attr",
                "is_device_attr",
                "device_id",
                "area_id",
                "area_name",
            ]
            hass_filters = ["closest", "expand", "device_id", "area_id", "area_name"]
            for glob in hass_globals:
                self.globals[glob] = unsupported(glob)
            for filt in hass_filters:
                self.filters[filt] = unsupported(filt)
            return

        self.globals["expand"] = hassfunction(expand)
        self.filters["expand"] = pass_context(self.globals["expand"])
        self.globals["closest"] = hassfunction(closest)
        self.filters["closest"] = pass_context(hassfunction(closest_filter))
        self.globals["distance"] = hassfunction(distance)
        self.globals["is_state"] = hassfunction(is_state)
        self.globals["is_state_attr"] = hassfunction(is_state_attr)
        self.globals["state_attr"] = hassfunction(state_attr)
        self.globals["states"] = AllStates(hass)
        self.globals["utcnow"] = hassfunction(utcnow)
        self.globals["now"] = hassfunction(now)

    def is_safe_callable(self, obj):
        """Test if callback is safe."""
        return isinstance(obj, AllStates) or super().is_safe_callable(obj)

    def is_safe_attribute(self, obj, attr, value):
        """Test if attribute is safe."""
        if isinstance(obj, (AllStates, DomainStates, TemplateState)):
            return attr[0] != "_"

        if isinstance(obj, Namespace):
            return True

        return super().is_safe_attribute(obj, attr, value)

    def compile(self, source, name=None, filename=None, raw=False, defer_init=False):
        """Compile the template."""
        if (
            name is not None
            or filename is not None
            or raw is not False
            or defer_init is not False
        ):
            # If there are any non-default keywords args, we do
            # not cache.  In prodution we currently do not have
            # any instance of this.
            return super().compile(source, name, filename, raw, defer_init)

        cached = self.template_cache.get(source)

        if cached is None:
            cached = self.template_cache[source] = super().compile(source)

        return cached


_NO_HASS_ENV = TemplateEnvironment(None)  # type: ignore[no-untyped-call]
