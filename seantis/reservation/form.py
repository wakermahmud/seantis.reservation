from datetime import datetime, date, time

from five import grok
from plone.directives import form
from z3c.form import interfaces

from seantis.reservation import utils
from seantis.reservation.resource import IResource

def extract_action_data(fn):
    """ Decorator which inserted after a buttonAndHandler directive will
    put the extracted data into a named tuple for easier access. 

    """
    def wrapper(self, action):
        data, errors = self.extractData()

        if errors:
            self.status = self.formErrorsMessage
            return

        return fn(self, utils.dictionary_to_namedtuple(data))

    return wrapper

def from_timestamp(fn):
    """ Decorator which inserted after a property will convert the return value
    from a timestamp to datetime. 

    """
    def converter(self, *args, **kwargs):
        try:
            date = fn(self, *args, **kwargs)
            return date and datetime.fromtimestamp(float(date)) or None
        except TypeError:
            return None

    return converter

class ResourceBaseForm(form.Form):
    """Baseform for all forms that work with resources as their context. 
    Provides helpful functions to all of them.

    """
    grok.baseclass()
    grok.context(IResource)
    ignoreContext = True
    
    hidden_fields = ['id']
    ignore_requirements = False

    def updateWidgets(self):
        super(ResourceBaseForm, self).updateWidgets()

        # Hide fields found in self.hidden_fields
        for field in self.hidden_fields:
            if field in self.widgets:
                self.widgets[field].mode = interfaces.HIDDEN_MODE
        
        # Forces the wigets to completely ignore requirements 
        if self.ignore_requirements:
            self.widgets.hasRequiredFields = False

    def defaults(self):
        return {}

    def redirect_to_context(self):
        """ Redirect to the url of the resource. """
        self.request.response.redirect(self.context.absolute_url())

    @property
    def scheduler(self):
        """ Returns the scheduler of the resource. """
        return self.context.scheduler()

    @property
    @from_timestamp
    def start(self):
        return self.request.get('start')

    @property
    @from_timestamp
    def end(self):
        return self.request.get('end')

    @property
    def group(self):
        return unicode(self.request.get('group', '').decode('utf-8'))

    def update(self, **kwargs):
        start, end = self.start, self.end
        if start and end:
            self.fields['day'].field.default = start.date()
            self.fields['start_time'].field.default = start.time()
            self.fields['end_time'].field.default = end.time()
        
        other_defaults = self.defaults()
        for k, v in other_defaults.items():
            self.fields[k].field.default = v

        super(ResourceBaseForm, self).update(**kwargs)