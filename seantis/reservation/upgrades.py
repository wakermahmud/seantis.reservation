from sqlalchemy import create_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.autogenerate import compare_metadata

from seantis.reservation import ORMBase
from seantis.reservation import utils

def upgrade_1_to_1000(setuptools):
    """ Upgrades seantis.reservation, adding modified and created dates to
    each table in the database.

    The upgrade uses alembic to create a diff between the existing database
    and the metadata.

    Alembic really is a tool for much more but for now this should cover it.
    Plus I'd like to be able to run the database upgrades through the
    add-on interface as administrator.

    """

    engine = create_engine(utils.get_config('dsn'), isolation_level='SERIALIZABLE')
    connection = engine.connect()
    trans = connection.begin()

    try:

        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, ORMBase.metadata)
        op = Operations(context)

        # go through diff and execute the changes on the operations object
        for method, table, col in diff:
            getattr(op, method)(table, col.copy())
    
        trans.commit()

    except:
        trans.rollback()
        raise