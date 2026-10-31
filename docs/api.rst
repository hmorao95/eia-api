API reference
=============

Clients
-------

.. autoclass:: eia_api.EIA
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: eia_api.AsyncEIA
   :members:
   :undoc-members:
   :show-inheritance:

Constants
---------

.. data:: eia_api.DEFAULT_CACHE_DIR

   Default value of every client's ``cache_dir`` argument: cache raw API
   responses in ``~/.cache/eia_api``. The home directory is looked up when the
   client is built, not when the package is imported. Pass ``cache_dir=None``
   (or ``""``) to disable the on-disk cache, or a path to use that directory.

Data objects
------------

.. autoclass:: eia_api.Route
   :no-members:

Command line
------------

.. autofunction:: eia_api.main
