import re
import time
import json
import random
import collections
import functools

from datetime import datetime, timedelta
from urlparse import urljoin
from urllib import quote
from itertools import tee, izip
from uuid import UUID
from uuid import uuid5 as new_uuid_mirror

import pytz

from plone.dexterity.utils import SchemaNameEncoder

from App.config import getConfiguration
from Acquisition import aq_inner
from zope.component import getMultiAdapter
from zope.component.hooks import getSite
from zope import i18n
from zope import interface
from Products.CMFCore.utils import getToolByName
from Products.CMFPlone.i18nl10n import weekdayname_msgid_abbr, monthname_msgid
from z3c.form.interfaces import ActionExecutionError
from plone.i18n.locales.languages import _languagelist

from seantis.reservation import error
from seantis.reservation import _

try:
    from collections import OrderedDict #python >= 2.7
except ImportError:
    from ordereddict import OrderedDict #python < 2.7

from seantis.reservation.sortedcollection import SortedCollection

dexterity_encoder = SchemaNameEncoder()

def profile(fn):
    """ Naive profiling of a function.. on unix systems only. """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.time()

        result = fn(*args, **kwargs)
        print fn.__name__, 'took', (time.time() - start) * 1000, 'ms'

        return result

    return wrapper

def additional_data_dictionary(data, fti):
    """ Takes the data from a post request and puts the relevant bits into
    a dictionary with the following structure:

    {
        "form_key": {
            "desc": "Form Description",
            "interface": "The interface used",
            "values": [
                {
                    "key": "value_key",
                    "sortkey": 1-n,
                    "value": "value",
                    "desc": "Value Description"
                },
                ...
            ]
        }
    }

    Interfaces used in the reservation data are the interfaces with the
    IReservationFormSet marker set.

    The dictionary is later converted to JSON and stored on the reservation.
    """

    result = dict()
    used_data = dict([(k, v) for k, v in data.items() if v])

    def values(iface, ifacekey):
        for key, value in used_data.items():
            if not key.startswith(ifacekey + '.'):
                continue

            subkey = key.split('.')[1]
            desc = iface.getDescriptionFor(subkey).title
            sortkey = iface.get(subkey).order

            yield dict(key=subkey, desc=desc, value=value, sortkey=sortkey)

    for key, info in fti.items():
        desc, iface = info[0], info[1]

        record = dict()
        record['desc'] = desc
        record['interface'] = iface.getName()
        record['values'] = list(values(iface, key))

        if not record['values']:
            continue

        result[key] = record

    return result

def mock_data_dictionary(data, formset_key='mock', formset_desc='Mocktest'):
    """ Given a dictionary of key values this function returns a dictionary
    with the same structure as additional_data_dictionary, without the use
    of interfaces.

    This function exists for testing purposes.
    """

    records = list()

    formset = dict()
    formset['desc'] = formset_desc
    formset['interface'] = 'mockinterface'
    formset['values'] = records

    for i, item in enumerate(data.items()):
        records.append(dict(key=item[0], sortkey=i, value=item[1], desc=item[0]))

    return {formset_key: formset}

def overlaps(start, end, otherstart, otherend):
    if otherstart <= start and start <= otherend:
        return True

    if start <= otherstart and otherstart <= end:
        return True

    return False

_requestid_expr = re.compile(r'\d')
def request_id_as_int(string):
    """Returns the id of a request as int without throwing an error if invalid
    characters are in the requested string (like ?id=11.11).

    """
    if string == None:
        return 0
        
    return int(''.join(re.findall(_requestid_expr, string)))

class memoize(object):
   """Decorator that caches a function's return value each time it is called.
   If called later with the same arguments, the cached value is returned, and
   not re-evaluated.
   """
   def __init__(self, func):
      self.func = func
      self.cache = {}
   def __call__(self, *args):
      try:
         return self.cache[args]
      except KeyError:
         value = self.func(*args)
         self.cache[args] = value
         return value
      except TypeError:
         # uncachable -- for instance, passing a list as an argument.
         # Better to not cache than to blow up entirely.
         return self.func(*args)
   def __repr__(self):
      """Return the function's docstring."""
      return self.func.__doc__
   def __get__(self, obj, objtype):
      """Support instance methods."""
      return functools.partial(self.__call__, obj)

@memoize
def _exposure():
    # keep direct imports out of utils as it will lead to circular imports
    from seantis.reservation import exposure
    return exposure

def compare_link(resources):
    """Builds the compare link for the given list of resources"""

    # limit the resources already on the link, as the view needs
    # at least two visible resources to be even callable
    # another check (for security is made in the resources module later)
    resources = _exposure().limit_resources(resources)

    if len(resources) < 2:
        return ''

    link = resources[0:1][0].absolute_url_path() + '?'
    compare_to = [r.uuid() for r in resources[1:]]

    for uuid in compare_to:
        link += 'compare_to=' + string_uuid(uuid) + '&'
        
    return link.rstrip('&')

def monthly_report_link(context, resources):
    """Builds the monthly report link given the list of the resources
    using the current year and month."""
    if not resources:
        return ''

    today = datetime.now()

    url = context.absolute_url()

    # note that 'monthly_report' is copied in reports.py
    url += '/monthly_report'
    url += '?year=' + str(today.year)
    url += '&month=' + str(today.month)

    for uuid in (r.uuid() for r in resources):
        url += '&uuid=' + string_uuid(uuid)

    return url

def get_current_language(context, request):
    """Returns the current language of the request"""
    context = aq_inner(context)
    portal_state = getMultiAdapter((context, request), name=u'plone_portal_state')
    return portal_state.language()

def get_current_site_language():
    """Returns the current language of the current site"""

    context = getSite()
    portal_languages = getToolByName(context, 'portal_languages')
    langs = portal_languages.getSupportedLanguages()

    return langs and langs[0] or u'en'

def get_site_email_sender():
    """Returns the default sender of emails of the current site in the
    following format: 'sendername<sender@domain>'.

    """

    site = getSite()
    address = site.getProperty('email_from_address')
    name = site.getProperty('email_from_name')

    if not address:
        return None

    if name:
        return '%s<%s>' % (name, address)
    else:
        return address

def translator(context, request):
    """Returns a function which takes a single string and translates it using
    the curried values for context & request.

    """
    def curried(text):
        return translate(context, request, text)
    return curried

def translate(context, request, text, domain=None):
    """Translates the given text using context & request."""
    lang = get_current_language(context, request)
    return i18n.translate(text, target_language=lang, domain=domain)

def translate_workflow(context, request, text):
    return translate(context, request, text, domain='plone')

def native_language_name(code):
    return _languagelist[code]['native']

def handle_action(action=None, success=None, message_handler=None):
    """Calls 'action' and then 'success' if everything went well or
    'message_handler' with a message if an exception happened.

    If no message_handler is given, an ActionExecutionError is raised
    with the message attached.

    """
    try:
        result = None
        if action: result = action()
        if success: success()

        return result

    except Exception, e:
        e = hasattr(e, 'orig') and e.orig or e
        handle_exception(e, message_handler)

def form_error(msg):
    raise ActionExecutionError(interface.Invalid(msg))

def handle_exception(ex, message_handler=None):
    msg = error.errormap.get(type(ex))

    if not msg:
        raise ex

    if message_handler:
        message_handler(msg)
    else:
        form_error(msg)

_uuid_regex = re.compile('[a-f0-9]{8}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{12}')
def is_uuid(obj):
    """Returns true if the given obj is a uuid. The obj may be a string
    or of type UUID. If it's a string, the uuid is checked with a regex.
    """
    if isinstance(obj, basestring):
        return re.match(_uuid_regex, unicode(obj))
    
    return isinstance(obj, UUID)

def string_uuid(uuid):
    return UUID(str(uuid)).hex

# TODO cache this incrementally
def generate_uuids(uuid, quota):
    mirror = lambda n: new_uuid_mirror(uuid, str(n))
    return [mirror(n) for n in xrange(1, quota)]

def uuid_query(uuid):
    """ Returns a tuple of uuids for querying the zodb. See why:
    http://stackoverflow.com/questions/10137632/querying-portal-catalog-using-typed-uuids-instead-of-string-uuids

    """

    uuid = string_uuid(uuid)

    if '-' in uuid:
        return (uuid.replace('-', ''), uuid)
    return (uuid, '-'.join([
        uuid[:8], uuid[8:12], uuid[12:16], uuid[16:20], uuid[20:]]))

def get_resource_by_uuid(context, uuid):
    """Returns the zodb object with the given uuid."""
    catalog = getToolByName(context, 'portal_catalog')
    results = catalog(UID=uuid_query(uuid))
    return len(results) == 1 and results[0] or None

def get_resource_title(resource):
    if hasattr(resource, '__parent__'):
        parent = resource.__parent__.title
    elif hasattr(resource, 'parent'):
        parent = resource.parent().title
    else:
        return resource.title

    return ' - '.join((parent, resource.title))

class UUIDEncoder(json.JSONEncoder):
    """Encodes UUID objects as string in JSON."""
    def default(self, obj):
        if isinstance(obj, UUID):
            return string_uuid(obj)
        return json.JSONEncoder.default(self, obj)

class SortedCollectionEncoder(json.JSONEncoder):
    """Encodes SortedCollectionEncoder objects as string in JSON."""
    def default(self, obj):
        if isinstance(obj, SortedCollection):
            return [o for o in obj]
        return json.JSONEncoder.default(self, obj)

def event_class(availability):
    """Returns the event class to be used depending on the availability."""
    if availability == 0:
        return 'event-unavailable'
    elif availability == 100:
        return 'event-available'
    else:
        return 'event-partly-available'

def event_availability(context, request, scheduler, allocation):
    a = allocation
    title = lambda msg: translate(context, request, msg)

    availability = scheduler.availability(a.start, a.end)

    if a.partly_available:
        if availability == 0:
            text = title(_(u'Occupied'))
        elif availability == 100:
            text = title(_(u'Free'))
        else:
            text = title(_(u'%i%% Free')) % availability
    else:
        spots = int(round(allocation.quota * availability / 100))
        if spots:
            if spots == 1:
                text = title(_(u'1 Spot Available'))
            else:
                text = title(_(u'%i Spots Available')) % spots
        else:
            text = title(_(u'No spots available'))

    if availability == 0:
        klass = 'event-fully-booked'
    else:
        klass = ''

    if allocation.approve:
        open_spots = allocation.open_waitinglist_spots()
        hint_availability = int( open_spots / float(allocation.waitinglist_spots) * 100.0)
        if open_spots:
            if open_spots == 1:
                text += '\n' + title(_(u'1 Waitinglist Spot'))
            else:
                text += '\n' + (title(_(u'%i Waitinglist Spots')) % open_spots)
        else:
            text += '\n' + title(_(u'Full Waitinglist'))
            klass = ('event-full-waitinglist' + ' ' + klass).strip()
    else:
        hint_availability = availability

    return text, (klass + ' ' + event_class(hint_availability)).strip()

def flatten(l):
    """Generator for flattening irregularly nested lists. 'Borrowed' from here:
    
    http://stackoverflow.com/questions/2158395/flatten-an-irregular-list-of-lists-in-python
    """
    for el in l:
        if isinstance(el, collections.Iterable) and not isinstance(el, basestring):
            for sub in flatten(el):
                yield sub
        else:
            yield el

def pairs(l):
    """Takes any list and returns pairs:
    ((a,b),(c,d)) => ((a,b),(c,d))
    (a,b,c,d) => ((a,b),(c,d))
    
    http://opensourcehacker.com/2011/02/23/tuplifying-a-list-or-pairs-in-python/
    """
    l = list(flatten(l))
    return zip(*[l[x::2] for x in (0,1)])

def pairwise(iterable):
    """Almost like paris, but not quite:
    "s -> (s0,s1), (s1,s2), (s2, s3), ..."
    """
    a, b = tee(iterable)
    next(b, None)
    return izip(a, b)

def get_config(key):
    config = getConfiguration()
    configuration = config.product_config.get('seantis.reservation', dict())
    return configuration.get(key)

# obsolete in python 2.7
def total_timedelta_seconds(td):
    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / 10**6

def utc_mktime(utc_tuple):
    """Returns number of seconds elapsed since epoch

    Note that no timezone are taken into consideration.

    utc tuple must be: (year, month, day, hour, minute, second)

    """

    if len(utc_tuple) == 6:
        utc_tuple += (0, 0, 0)
    return time.mktime(utc_tuple) - time.mktime((1970, 1, 1, 0, 0, 0, 0, 0, 0))

def utctimestamp(dt):
    dt = dt.replace(tzinfo=pytz.utc)
    return utc_mktime(dt.timetuple())   

def merge_reserved_slots(slots):
    """Given a list of reserved slots a list of tuples with from-to datetimes is
    formed, with adjacent slots being combined into one continious timespan. 
    Usually this leaves reserved_slots be, but if the slots in the list come from
    a partially available allocation the reserved_slots of such an allocation can
    be merged into one timespan.

    So instead of
    08:00 - 08:14:59:59
    08:15 - 08:29:59:59

    You'll get
    08:00 - 08:30

    for this, the display_start and display_end of ResourceSlot are used."""

    class Timespan(object):
        def __init__(self, start, end):
            self.start = start
            self.end = end

    if len(slots) == 1:
        return [Timespan(slots[0].start, slots[0].end)]

    slots = sorted(slots, key=lambda s: s.start)
    
    merged = []
    current = Timespan(None, None)

    for this, next in pairwise(slots):
        
        if abs((next.start - this.end).seconds) <= 1:
            current.start = current.start and current.start or this.start
            current.end = next.end
        else:
            merged.append(current)
            current = Timespan(None, None)

    if current.start:
        merged.append(current)

    return merged

def urlparam(base, url, params):
    """Joins an url, adding parameters as query parameters."""
    if not base.endswith('/'): base += '/'

    urlquote = lambda fragment: quote(unicode(fragment).encode('utf-8'))
    querypair = lambda pair: pair[0] + '=' + urlquote(pair[1])

    query = '?' + '&'.join(map(querypair, params.items()))
    return ''.join(reduce(urljoin, (base, url, query)))

class EventUrls(object):
    def __init__(self, resource, request, exposure):
        self.resource = resource
        self.base = resource.absolute_url_path()
        self.request = request
        self.translate = translator(resource, request)
        self.menu = {}
        self.order = []
        self.exposure = exposure
        self.default = ""
        self.move = ""
    
    @memoize
    def restricted_url(self, view):
        """Returns a function which can be used to build an url with optional
        parameters. The function only builds the url if the current user has
        the right to do so.

        """
        base = self.resource.absolute_url_path()
        is_exposed = self.exposure.for_views(self.resource, self.request)

        def build(params):
            if is_exposed(view):
                return urlparam(base, view, params)
            else:
                return None

        # return closure
        return build

    @memoize
    def get_restricted(self, view, params):
        urlfactory = self.restricted_url(view)
        if not urlfactory: return None

        return urlfactory(params)

    def menu_add(self, group, name, view, params, target):
        url = self.get_restricted(view, params)
        if not url: return

        group = self.translate(group)
        name = self.translate(name)

        if not group in self.menu:
            self.menu[group] = []
            self.order.append(group)

        self.menu[group].append(dict(name=name, url=url, target=target))

    def default_url(self, view, params):
        url = self.get_restricted(view, params)
        if not url: return

        self.default = url

    def move_url(self, view, params):
        url = self.get_restricted(view, params)
        if not url: return

        self.move = url

def get_date_range(day, start_time, end_time):
    """Returns the date-range of a date a start and an end time."""
    start = datetime.combine(day, start_time)
    end = datetime.combine(day, end_time)

    # since the user can only one date with separate times it is assumed
    # that an end before a start is meant for the following day
    if end < start: end += timedelta(days=1)

    return start, end

def flash(context, message, type='info'):
    context.plone_utils.addPortalMessage(message, type)

def month_name(context, request, month):
    """Returns the text for the given month (1-12)."""
    assert (1 <= month and month <= 12)
    msgid = monthname_msgid(month)
    return translate(context, request, msgid, 'plonelocales')

def weekdayname_abbr(context, request, day):
    """Returns the text for the given day (0-6), 0 being sunday.
    Since 0 is sunday in the plone function and monday in the python stdlib
    you might want to shift the day.weekday() result"""
    assert (0 <= day and day <= 6)
    msgid = weekdayname_msgid_abbr(day)
    return translate(context, request, msgid, 'plonelocales')

def shift_day(stdday):
    return abs(0 - (stdday+1) % 7)

def portal_type_in_context(context, portal_type):
    """Returns the given portal types _within_ the current context."""
    path = '/'.join(context.getPhysicalPath())

    catalog = getToolByName(context, 'portal_catalog')
    results = catalog(
            portal_type = portal_type,
            path={'query': path, 'depth': 1}
        )
    return results

def portal_type_by_context(context, portal_type):
    """Returns the given portal_type _for_ the current context. Portal types 
    for a context are required by traversing up the acquisition context.

    """

    # As this code is used for backend stuff that needs to be callable for
    # every user the idea is to do an unrestricted traverse.

    # Calling unrestrictedTraverse first seems to work with the supposedly
    # restricted catalog.

    if hasattr(context, 'getPath'):
        path = context.getPath()
    else:
        path = context.getPhysicalPath()
    
    context = context.aq_inner.unrestrictedTraverse(path)

    def traverse(context, portal_type):
        frames = portal_type_in_context(context, portal_type)
        if frames:
            return [f.getObject() for f in frames]
        else:
            if not hasattr(context, 'portal_type'):
                return []
            if context.portal_type == 'Plone Site':
                return []
            
            parent = context.aq_inner.aq_parent
            return traverse(parent, portal_type)

    return traverse(context, portal_type)