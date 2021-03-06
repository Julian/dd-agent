"""Base class for Checks.

If you are writing your own checks you should subclass the AgentCheck class.
The Check class is being deprecated so don't write new checks with it.
"""

import logging
import re
import socket
import time
import types
import os
import sys
import traceback
from pprint import pprint

from util import LaconicFilter, get_os, get_hostname
from config import get_confd_path
from checks import check_status

log = logging.getLogger(__name__)

# Konstants
class CheckException(Exception): pass
class Infinity(CheckException): pass
class NaN(CheckException): pass
class UnknownValue(CheckException): pass



#==============================================================================
# DEPRECATED
# ------------------------------
# If you are writing your own check, you should inherit from AgentCheck
# and not this class. This class will be removed in a future version 
# of the agent.
#==============================================================================
class Check(object):
    """
    (Abstract) class for all checks with the ability to:
    * store 1 (and only 1) sample for gauges per metric/tag combination
    * compute rates for counters
    * only log error messages once (instead of each time they occur)

    """


    def __init__(self, logger):
        # where to store samples, indexed by metric_name
        # metric_name: {("sorted", "tags"): [(ts, value), (ts, value)],
        #                 tuple(tags) are stored as a key since lists are not hashable
        #               None: [(ts, value), (ts, value)]}
        #                 untagged values are indexed by None
        self._sample_store = {}
        self._counters = {} # metric_name: bool
        self.logger = logger
        try:
            self.logger.addFilter(LaconicFilter())
        except:
            self.logger.exception("Trying to install laconic log filter and failed")

    def normalize(self, metric, prefix=None):
        """Turn a metric into a well-formed metric name
        prefix.b.c
        """
        name = re.sub(r"[,\+\*\-/()\[\]{}]", "_", metric)
        # Eliminate multiple _
        name = re.sub(r"__+", "_", name)
        # Don't start/end with _
        name = re.sub(r"^_", "", name)
        name = re.sub(r"_$", "", name)
        # Drop ._ and _.
        name = re.sub(r"\._", ".", name)
        name = re.sub(r"_\.", ".", name)

        if prefix is not None:
            return prefix + "." + name
        else:
            return name

    def normalize_device_name(self, device_name):
        return device_name.strip().lower().replace(' ', '_')

    def counter(self, metric):
        """
        Treats the metric as a counter, i.e. computes its per second derivative
        ACHTUNG: Resets previous values associated with this metric.
        """
        self._counters[metric] = True
        self._sample_store[metric] = {}

    def is_counter(self, metric):
        "Is this metric a counter?"
        return metric in self._counters

    def gauge(self, metric):
        """
        Treats the metric as a gauge, i.e. keep the data as is
        ACHTUNG: Resets previous values associated with this metric.
        """
        self._sample_store[metric] = {}

    def is_metric(self, metric):
        return metric in self._sample_store

    def is_gauge(self, metric):
        return self.is_metric(metric) and \
               not self.is_counter(metric)

    def get_metric_names(self):
        "Get all metric names"
        return self._sample_store.keys()

    def save_gauge(self, metric, value, timestamp=None, tags=None, hostname=None, device_name=None):
        """ Save a gauge value. """
        if not self.is_gauge(metric):
            self.gauge(metric)
        self.save_sample(metric, value, timestamp, tags, hostname, device_name)

    def save_sample(self, metric, value, timestamp=None, tags=None, hostname=None, device_name=None):
        """Save a simple sample, evict old values if needed
        """
        from util import cast_metric_val

        if timestamp is None:
            timestamp = time.time()
        if metric not in self._sample_store:
            raise CheckException("Saving a sample for an undefined metric: %s" % metric)
        try:
            value = cast_metric_val(value)
        except ValueError, ve:
            raise NaN(ve)

        # Sort and validate tags
        if tags is not None:
            if type(tags) not in [type([]), type(())]:
                raise CheckException("Tags must be a list or tuple of strings")
            else:
                tags = tuple(sorted(tags))

        # Data eviction rules
        key = (tags, device_name)
        if self.is_gauge(metric):
            self._sample_store[metric][key] = ((timestamp, value, hostname, device_name), )
        elif self.is_counter(metric):
            if self._sample_store[metric].get(key) is None:
                self._sample_store[metric][key] = [(timestamp, value, hostname, device_name)]
            else:
                self._sample_store[metric][key] = self._sample_store[metric][key][-1:] + [(timestamp, value, hostname, device_name)]
        else:
            raise CheckException("%s must be either gauge or counter, skipping sample at %s" % (metric, time.ctime(timestamp)))

        if self.is_gauge(metric):
            # store[metric][tags] = (ts, val) - only 1 value allowed
            assert len(self._sample_store[metric][key]) == 1, self._sample_store[metric]
        elif self.is_counter(metric):
            assert len(self._sample_store[metric][key]) in (1, 2), self._sample_store[metric]

    @classmethod
    def _rate(cls, sample1, sample2):
        "Simple rate"
        try:
            interval = sample2[0] - sample1[0]
            if interval == 0:
                raise Infinity()

            delta = sample2[1] - sample1[1]
            if delta < 0:
                raise UnknownValue()

            return (sample2[0], delta / interval, sample2[2], sample2[3])
        except Infinity:
            raise 
        except UnknownValue:
            raise 
        except Exception, e:
            raise NaN(e)

    def get_sample_with_timestamp(self, metric, tags=None, device_name=None, expire=True):
        "Get (timestamp-epoch-style, value)"

        # Get the proper tags
        if tags is not None and type(tags) == type([]):
            tags.sort()
            tags = tuple(tags)
        key = (tags, device_name)

        # Never seen this metric
        if metric not in self._sample_store:
            raise UnknownValue()

        # Not enough value to compute rate
        elif self.is_counter(metric) and len(self._sample_store[metric][key]) < 2:
           raise UnknownValue()

        elif self.is_counter(metric) and len(self._sample_store[metric][key]) >= 2:
            res = self._rate(self._sample_store[metric][key][-2], self._sample_store[metric][key][-1])
            if expire:
                del self._sample_store[metric][key][:-1]
            return res

        elif self.is_gauge(metric) and len(self._sample_store[metric][key]) >= 1:
            return self._sample_store[metric][key][-1]

        else:
            raise UnknownValue()

    def get_sample(self, metric, tags=None, device_name=None, expire=True):
        "Return the last value for that metric"
        x = self.get_sample_with_timestamp(metric, tags, device_name, expire)
        assert type(x) == types.TupleType and len(x) == 4, x
        return x[1]

    def get_samples_with_timestamps(self, expire=True):
        "Return all values {metric: (ts, value)} for non-tagged metrics"
        values = {}
        for m in self._sample_store:
            try:
                values[m] = self.get_sample_with_timestamp(m, expire=expire)
            except:
                pass
        return values

    def get_samples(self, expire=True):
        "Return all values {metric: value} for non-tagged metrics"
        values = {}
        for m in self._sample_store:
            try:
                # Discard the timestamp
                values[m] = self.get_sample_with_timestamp(m, expire=expire)[1]
            except:
                pass
        return values

    def get_metrics(self, expire=True):
        """Get all metrics, including the ones that are tagged.
        This is the preferred method to retrieve metrics

        @return the list of samples
        @rtype [(metric_name, timestamp, value, {"tags": ["tag1", "tag2"]}), ...]
        """
        metrics = []
        for m in self._sample_store:
            try:
                for key in self._sample_store[m]:
                    tags, device_name = key
                    try:
                        ts, val, hostname, device_name = self.get_sample_with_timestamp(m, tags, device_name, expire)
                    except UnknownValue:
                        continue
                    attributes = {}
                    if tags:
                        attributes['tags'] = list(tags)
                    if hostname:
                        attributes['host_name'] = hostname
                    if device_name:
                        attributes['device_name'] = device_name
                    metrics.append((m, int(ts), val, attributes))
            except:
                pass
        return metrics

class AgentCheck(object):

    def __init__(self, name, init_config, agentConfig, instances=None):
        """
        Initialize a new check.

        :param name: The name of the check
        :param init_config: The config for initializing the check
        :param agentConfig: The global configuration for the agent
        :param instances: A list of configuration objects for each instance.
        """
        from aggregator import MetricsAggregator


        self.name = name
        self.init_config = init_config
        self.agentConfig = agentConfig
        self.hostname = get_hostname(agentConfig)
        self.log = logging.getLogger('%s.%s' % (__name__, name))

        self.aggregator = MetricsAggregator(self.hostname, formatter=agent_formatter, recent_point_threshold=agentConfig.get('recent_point_threshold', None))

        self.events = []
        self.instances = instances or []
        self.warnings = []

    def instance_count(self):
        """ Return the number of instances that are configured for this check. """
        return len(self.instances)

    def gauge(self, metric, value, tags=None, hostname=None, device_name=None, timestamp=None):
        """
        Record the value of a gauge, with optional tags, hostname and device
        name.

        :param metric: The name of the metric
        :param value: The value of the gauge
        :param tags: (optional) A list of tags for this metric
        :param hostname: (optional) A hostname for this metric. Defaults to the current hostname.
        :param device_name: (optional) The device name for this metric
        :param timestamp: (optional) The timestamp for this metric value
        """
        self.aggregator.gauge(metric, value, tags, hostname, device_name, timestamp)

    def increment(self, metric, value=1, tags=None, hostname=None, device_name=None):
        """
        Increment a counter with optional tags, hostname and device name.

        :param metric: The name of the metric
        :param value: The value to increment by
        :param tags: (optional) A list of tags for this metric
        :param hostname: (optional) A hostname for this metric. Defaults to the current hostname.
        :param device_name: (optional) The device name for this metric
        """
        self.aggregator.increment(metric, value, tags, hostname, device_name)

    def decrement(self, metric, value=-1, tags=None, hostname=None, device_name=None):
        """
        Increment a counter with optional tags, hostname and device name.

        :param metric: The name of the metric
        :param value: The value to decrement by
        :param tags: (optional) A list of tags for this metric
        :param hostname: (optional) A hostname for this metric. Defaults to the current hostname.
        :param device_name: (optional) The device name for this metric
        """
        self.aggregator.decrement(metric, value, tags, hostname, device_name)

    def rate(self, metric, value, tags=None, hostname=None, device_name=None):
        """
        Submit a point for a metric that will be calculated as a rate on flush.
        Values will persist across each call to `check` if there is not enough
        point to generate a rate on the flush.

        :param metric: The name of the metric
        :param value: The value of the rate
        :param tags: (optional) A list of tags for this metric
        :param hostname: (optional) A hostname for this metric. Defaults to the current hostname.
        :param device_name: (optional) The device name for this metric
        """
        self.aggregator.rate(metric, value, tags, hostname, device_name)

    def histogram(self, metric, value, tags=None, hostname=None, device_name=None):
        """
        Sample a histogram value, with optional tags, hostname and device name.

        :param metric: The name of the metric
        :param value: The value to sample for the histogram
        :param tags: (optional) A list of tags for this metric
        :param hostname: (optional) A hostname for this metric. Defaults to the current hostname.
        :param device_name: (optional) The device name for this metric
        """
        self.aggregator.histogram(metric, value, tags, hostname, device_name)

    def set(self, metric, value, tags=None, hostname=None, device_name=None):
        """
        Sample a set value, with optional tags, hostname and device name.

        :param metric: The name of the metric
        :param value: The value for the set
        :param tags: (optional) A list of tags for this metric
        :param hostname: (optional) A hostname for this metric. Defaults to the current hostname.
        :param device_name: (optional) The device name for this metric
        """
        self.aggregator.set(metric, value, tags, hostname, device_name)

    def event(self, event):
        """
        Save an event.

        :param event: The event payload as a dictionary. Has the following
        structure:

            {
                "timestamp": int, the epoch timestamp for the event,
                "event_type": string, the event time name,
                "api_key": string, the api key of the account to associate the event with,
                "msg_title": string, the title of the event,
                "msg_text": string, the text body of the event,
                "alert_type": (optional) string, one of ('error', 'warning', 'success', 'info').
                    Defaults to 'info'.
                "source_type_name": (optional) string, the source type name,
                "host": (optional) string, the name of the host,
                "tags": (optional) list, a list of tags to associate with this event
            }
        """
        self.events.append(event)

    def has_events(self):
        """
        Check whether the check has saved any events

        @return whether or not the check has saved any events
        @rtype boolean
        """
        return len(self.events) > 0

    def get_metrics(self):
        """
        Get all metrics, including the ones that are tagged.

        @return the list of samples
        @rtype [(metric_name, timestamp, value, {"tags": ["tag1", "tag2"]}), ...]
        """
        return self.aggregator.flush()

    def get_events(self):
        """
        Return a list of the events saved by the check, if any

        @return the list of events saved by this check
        @rtype list of event dictionaries
        """
        events = self.events
        self.events = []
        return events

    def has_warnings(self):
        """
        Check whether the instance run created any warnings
        """
        return len(self.warnings) > 0

    def warning(self, warning_message):
        """ Add a warning message that will be printed in the info page 
        :param warning_message: String. Warning message to be displayed
        """
        self.warnings.append(warning_message)

    def get_warnings(self):
        """
        Return the list of warnings messages to be displayed in the info page
        """
        warnings = self.warnings
        self.warnings = []
        return warnings

    def run(self):
        """ Run all instances. """
        instance_statuses = []
        for i, instance in enumerate(self.instances):
            try:
                self.check(instance)
                if self.has_warnings():
                    instance_status = check_status.InstanceStatus(i, 
                        check_status.STATUS_WARNING, 
                        warnings=self.get_warnings()
                    )
                else:
                    instance_status = check_status.InstanceStatus(i, check_status.STATUS_OK)
            except Exception, e:
                self.log.exception("Check '%s' instance #%s failed" % (self.name, i))
                instance_status = check_status.InstanceStatus(i, 
                    check_status.STATUS_ERROR, 
                    error=e,
                    tb=traceback.format_exc()
                )
            instance_statuses.append(instance_status)
        return instance_statuses

    def check(self, instance):
        """
        Overriden by the check class. This will be called to run the check.

        :param instance: A dict with the instance information. This will vary
        depending on your config structure.
        """
        raise NotImplementedError()

    def stop(self):
        """
        To be executed when the agent is being stopped to clean ressources
        """
        pass

    @classmethod
    def from_yaml(cls, path_to_yaml=None, agentConfig=None, yaml_text=None, check_name=None):
        """
        A method used for testing your check without running the agent.
        """
        from util import yaml, yLoader
        if path_to_yaml:
            check_name = os.path.basename(path_to_yaml).split('.')[0]
            try:
                f = open(path_to_yaml)
            except IOError:
                raise Exception('Unable to open yaml config: %s' % path_to_yaml)
            yaml_text = f.read()
            f.close()

        config = yaml.load(yaml_text, Loader=yLoader)
        check = cls(check_name, config.get('init_config') or {}, agentConfig or {})

        return check, config.get('instances', [])

    def normalize(self, metric, prefix=None):
        """
        Turn a metric into a well-formed metric name
        prefix.b.c

        :param metric The metric name to normalize
        :param prefix A prefix to to add to the normalized name, default None
        """
        name = re.sub(r"[,\+\*\-/()\[\]{}]", "_", metric)
        # Eliminate multiple _
        name = re.sub(r"__+", "_", name)
        # Don't start/end with _
        name = re.sub(r"^_", "", name)
        name = re.sub(r"_$", "", name)
        # Drop ._ and _.
        name = re.sub(r"\._", ".", name)
        name = re.sub(r"_\.", ".", name)

        if prefix is not None:
            return prefix + "." + name
        else:
            return name


def agent_formatter(metric, value, timestamp, tags, hostname, device_name=None):
    """ Formats metrics coming from the MetricsAggregator. Will look like:
     (metric, timestamp, value, {"tags": ["tag1", "tag2"], ...})
    """
    attributes = {}
    if tags:
        attributes['tags'] = list(tags)
    if hostname:
        attributes['hostname'] = hostname
    if device_name:
        attributes['device_name'] = device_name
    if attributes:
        return (metric, int(timestamp), value, attributes)
    return (metric, int(timestamp), value)


def run_check(name, path=None):
    from tests.common import get_check

    # Read the config file
    confd_path = path or os.path.join(get_confd_path(get_os()), '%s.yaml' % name)

    try:
        f = open(confd_path)
    except IOError:
        raise Exception('Unable to open configuration at %s' % confd_path)

    config_str = f.read()
    f.close()

    # Run the check
    check, instances = get_check(name, config_str)
    if not instances:
        raise Exception('YAML configuration returned no instances.')
    for instance in instances:
        check.check(instance)
        if check.has_events():
            print "Events:\n"
            pprint(check.get_events(), indent=4)
        print "Metrics:\n"
        pprint(check.get_metrics(), indent=4)
