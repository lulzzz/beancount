"""
Web server for Beancount ledgers.
This uses the Bottle single-file micro web framework (with no plugins).
"""
import argparse
import time
import datetime
from os import path
from textwrap import dedent
import copy
import io
import re
import functools
from collections import defaultdict
from collections import defaultdict

import bottle
from bottle import install, response, request

from beancount2.web.bottle_utils import AttrMapper, internal_redirect

from beancount2 import parser
from beancount2 import validation
from beancount2 import data
from beancount2.data import account_leaf_name, is_account_root
from beancount2.data import Account, Lot
from beancount2.parser import get_account_types
from beancount2.balance import get_balance_amount
from beancount2 import realization
from beancount2.realization import RealAccount
from beancount2 import summarize
from beancount2 import utils
from beancount2.utils import index_key
from beancount2.inventory import Inventory
from beancount2.data import Open, Close, Pad, Check, Transaction, Event, Note, Price, Posting


#--------------------------------------------------------------------------------
# Global application pages.


app = bottle.Bottle()
A = AttrMapper(app.router.build)


def render_global(*args, **kw):
    """Render the title and contents in our standard template."""
    kw['A'] = A # Application mapper
    kw['V'] = V # View mapper
    kw['title'] = app.contents.options['title']
    kw['view_title'] = ''
    kw['navigation'] = GLOBAL_NAVIGATION
    return template.render(*args, **kw)


@app.route('/', name='root')
def root():
    "Redirect the root page to the home page."
    bottle.redirect(app.get_url('toc'))


@app.route('/toc', name='toc')
def toc():
    mindate, maxdate = data.get_min_max_dates([entry for entry in clean_entries
                                               if not isinstance(entry, (Open, Close))])

    views = []
    views.append((app.router.build('all', path=''),
                  'All Transactions'))

    for year in reversed(list(data.get_active_years(contents.entries))):
        views.append((app.get_url('year', path='', year=year),
                      'Year: {}'.format(year)))

    view_items = ['<li><a href="{}">{}</a></li>'.format(url, title)
                  for url, title in views]
    return render_global(
        pagetitle = "Table of Contents",
        contents = """
          <h2>Views</h2>
          <ul>
            {view_items}
          </ul>
        """.format(view_items='\n'.join(view_items)))


@app.route('/errors', name='errors')
def errors():
    "Report error encountered during parsing, checking and realization."
    return render_global(
        pagetitle = "Errors",
        contents = ""
        )


@app.route('/stats', name='stats')
def stats():
    "Compute and render statistics about the input file."
    # Note: maybe the contents of this can fit on the home page, if this is simple.
    return render_global(
        pagetitle = "Statistics",
        contents = ""
        )


@app.route('/source', name='source')
def source():
    "Render the source file, allowing scrolling at a specific line."
    return render_global(
        pagetitle = "Source",
        contents = ""
        )


@app.route('/update', name='update')
def update():
    "Render the update activity."
    return render_global(
        pagetitle = "Update Activity",
        contents = ""
        )


@app.route('/events', name='events')
def update():
    "Render an index for the various kinds of events."
    return render_global(
        pagetitle = "Events",
        contents = ""
        )


@app.route('/prices', name='prices')
def prices():
    "Render information about prices."
    return render_global(
        pagetitle = "Prices",
        contents = ""
        )


GLOBAL_NAVIGATION = bottle.SimpleTemplate("""
<ul>
  <li><a href="{{A.toc}}">Table of Contents</a></li>
  <li><a href="{{A.errors}}">Errors</a></li>
  <li><a href="{{A.source}}">Source</a></li>
  <li><a href="{{A.stats}}">Statistics</a></li>
  <li><a href="{{A.update}}">Update Activity</a></li>
  <li><a href="{{A.events}}">Events</a></li>
  <li><a href="{{A.prices}}">Prices</a></li>
</ul>
""").render(A=A)


@app.route('/style.css', name='style')
def style():
    "Stylesheet for the entire document."
    response.content_type = 'text/css'
    if app.args.debug:
        with open(path.join(path.dirname(__file__), 'style.css')) as f:
            global STYLE; STYLE = f.read()
    return STYLE


#--------------------------------------------------------------------------------
# Realization application pages.


viewapp = bottle.Bottle()
V = AttrMapper(lambda *args, **kw: request.app.get_url(*args, **kw))


def handle_view(path_depth):
    """A decorator for handlers which create views lazily.
    If you decorate a method with this, the wrapper does the redirect
    handling and your method is just a factory for a View instance,
    which is cached."""

    def view_populator(callback):
        def wrapper(*args, **kwargs):
            components = request.path.split('/')
            viewid = '/'.join(components[:path_depth+1])
            try:
                # Try fetching the view from the cache.
                view = app.views[viewid]
            except KeyError:
                # We need to create the view.
                view = app.views[viewid] = callback(*args, **kwargs)

            # Save for hte subrequest and redirect. populate_view() picks this
            # up and saves it in request.view.
            request.environ['VIEW'] = view
            return internal_redirect(viewapp, path_depth)
        return wrapper
    return view_populator


def populate_view(callback):
    "A plugin that will populate the request with the current view instance."
    def wrapper(*args, **kwargs):
        request.view = request.environ['VIEW']
        return callback(*args, **kwargs)
    return wrapper

viewapp.install(populate_view)


def render_app(*args, **kw):
    """Render the title and contents in our standard template."""
    kw['A'] = A # Application mapper
    kw['V'] = V # View mapper
    kw['title'] = app.contents.options['title']
    kw['view_title'] = ' - ' + request.view.title
    kw['navigation'] = APP_NAVIGATION.render(A=A, V=V, view_title=request.view.title)
    return template.render(*args, **kw)

APP_NAVIGATION = bottle.SimpleTemplate("""
<ul>
  <li><a href="{{A.toc}}">Table of Contents</a></li>
  <li><span class="ledger-name">{{view_title}}:</span></li>
  <li><a href="{{V.openbal}}">Opening Balances</a></li>
  <li><a href="{{V.balsheet}}">Balance Sheet</a></li>
  <li><a href="{{V.income}}">Income Statement</a></li>
  <li><a href="{{V.trial}}">Trial Balance</a></li>
  <li><a href="{{V.journal}}">Journal</a></li>
  <li><a href="{{V.positions}}">Positions</a></li>
  <li><a href="{{V.conversions}}">Conversions</a></li>
  <li><a href="{{V.documents}}">Documents</a></li>
</ul>
""")


@viewapp.route('/', name='approot')
def approot():
    bottle.redirect(request.app.get_url('balsheet'))









EMS_PER_SPACE = 3
_account_link_cache = {}

def account_link(account_name, leafonly=False):
    "Render an anchor for the given account name."
    if isinstance(account_name, (Account, RealAccount)):
        account_name = account_name.name
    try:
        return _account_link_cache[(request.app, account_name)]
    except KeyError:
        slashed_name = account_name.replace(':', '/')

        if leafonly:
            account_name = account_leaf_name(account_name)

        link = '<a href="{}" class="account">{}</a>'.format(
            request.app.get_url('account', slashed_account_name=slashed_name),
            account_name)
        _account_link_cache[account_name] = link
        return link


def tree_table(oss, tree, start_node_name, header=None, classes=None):
    """Generator to a tree of accounts as an HTML table.
    Render only all the nodes under 'start_node_name'.
    This yields the real_account object for each line and a
    list object used to return the values for multiple cells.
    """
    write = lambda data: (oss.write(data), oss.write('\n'))

    write('<table class="tree-table {}">'.format(
        ' '.join(classes) if classes else ''))

    if header:
        write('<thead>')
        write('</tr>')
        header_iter = iter(header)
        write('<th class="first">{}</th>'.format(next(header_iter)))
        for column in header_iter:
            write('<th>{}</th>'.format(column))
        write('</tr>')
        write('</thead>')

    if start_node_name not in tree:
        write('</table>')
        return

    lines = list(tree.render_lines(start_node_name))
    for line_first, _, account_name, real_account in lines:

        # Let the caller fill in the data to be rendered by adding it to a list
        # objects. The caller may return multiple cell values; this will create
        # multiple columns.
        cells = []
        row_classes = []
        yield real_account, cells, row_classes

        # If no cells were added, skip the line. If you want to render empty
        # cells, append empty strings.
        if not cells:
            continue

        # Render the row
        write('<tr class="{}">'.format(' '.join(row_classes)))
        write('<td class="tree-node-name" style="padding-left: {}em">{}</td>'.format(
            len(line_first)/EMS_PER_SPACE,
            account_link(real_account, leafonly=True)))

        # Add columns for each value rendered.
        for cell in cells:
            write('<td class="num">{}</td>'.format(cell))

        write('</tr>')

    write('</table>')


def is_account_active(real_account):
    """Return true if the account should be rendered. An inactive account only has
    an Open directive and nothing else."""

    for entry in real_account.postings:
        if isinstance(entry, Open):
            continue
        return True
    return False


def table_of_balances(tree, start_node_name, currencies, classes=None):
    """Render a table of balances."""

    header = ['Account'] + currencies + ['Other']

    # Pre-calculate which accounts should be rendered.
    active_accounts = tree.mark_from_leaves(is_account_active)
    active_set = set(real_account.name for real_account in active_accounts)

    oss = io.StringIO()
    for real_account, cells, row_classes in tree_table(oss, tree, start_node_name,
                                                       header, classes):

        # Check if this account has had activity; if not, skip rendering it.
        if (real_account.name not in active_set and
            not is_account_root(real_account.name)):
            continue

        if real_account.account is None:
            row_classes.append('parent-node')

        # For each account line, get the final balance of the account (at cost).
        balance_cost = real_account.balance.get_cost()

        # Extract all the positions that the user has identified as home
        # currencies.
        positions = list(balance_cost.get_positions())
        for currency in currencies:
            position = balance_cost.get_position(Lot(currency, None, None))
            if position:
                positions.remove(position)
                cells.append('{:,.2f}'.format(position.number))
            else:
                cells.append('')

        # Render all the rest of the inventory in the last cell.
        cells.append('\n<br/>'.join(map(str, positions)))

    return oss.getvalue()





@viewapp.route('/trial', name='trial')
def trial():
    "Trial balance / Chart of Accounts."

    view = request.view
    real_accounts = view.real_accounts
    operating_currencies = view.options['operating_currency']
    table = table_of_balances(real_accounts, '', operating_currencies,
                              classes=['trial'])


    ## FIXME: After conversions is fixed, this should always be zero.
    total_balance = realization.compute_total_balance(view.entries)
    table += """
      Total Balance: <span class="num">{}</span>
    """.format(total_balance.get_cost())

    return render_app(
        pagetitle = "Trial Balance",
        contents = table
        )


def balance_sheet_table(real_accounts, options):
    """Render an HTML balance sheet of the real_accounts tree."""

    operating_currencies = options['operating_currency']
    assets      = table_of_balances(real_accounts, options['name_assets'], operating_currencies)
    liabilities = table_of_balances(real_accounts, options['name_liabilities'], operating_currencies)
    equity      = table_of_balances(real_accounts, options['name_equity'], operating_currencies)

    return """
           <div id="assets" class="halfleft">
            <h3>Assets</h3>
            {assets}
           </div>

           <div id="liabilities" class="halfright">
            <h3>Liabilities</h3>
            {liabilities}
           </div>

           <div class="spacer halfright">
           </div>

           <div id="equity" class="halfright">
            <h3>Equity</h3>
            {equity}
           </div>
        """.format(**vars())


@viewapp.route('/balsheet', name='balsheet')
def balsheet():
    "Balance sheet."

    view = request.view
    real_accounts = request.view.closing_real_accounts
    contents = balance_sheet_table(real_accounts, view.options)

    return render_app(pagetitle = "Balance Sheet",
                      contents = contents)


@viewapp.route('/openbal', name='openbal')
def openbal():
    "Opening balances."

    view = request.view
    real_accounts = request.view.opening_real_accounts
    if real_accounts is None:
        contents = 'N/A'
    else:
        contents = balance_sheet_table(real_accounts, view.options)

    return render_app(pagetitle = "Opening Balances",
                      contents = contents)


@viewapp.route('/income', name='income')
def income():
    "Income statement."

    view = request.view
    real_accounts = request.view.real_accounts

    # Render the income statement tables.
    operating_currencies = view.options['operating_currency']
    income   = table_of_balances(real_accounts, view.options['name_income'], operating_currencies)
    expenses = table_of_balances(real_accounts, view.options['name_expenses'], operating_currencies)

    contents = """
       <div id="income" class="halfleft">
        <h3>Income</h3>
        {income}
       </div>

       <div id="expenses" class="halfright">
        <h3>Expenses</h3>
        {expenses}
       </div>
    """.format(**vars())

    return render_app(pagetitle = "Income Statement",
                      contents = contents)















## FIXME: This deserves to be somewhere else, I'm thinking realization.py
def iterate_with_balance(entries):
    """Iterate over the entries accumulating the balance.
    For each entry, it yields

      (entry, change, balance)

    'entry' is the entry for this line. If the list contained Posting instance,
    this yields the corresponding Transaction object.

    'change' is an Inventory object that reflects the change due to this entry
    (this may be multiple positions in the case that a single transaction has
    multiple legs).

    The 'balance' yielded is never None; it's up to the one displaying the entry
    to decide whether to render for a particular type.

    Also, multiple postings for the same transaction are de-duped
    and when a Posting is encountered, the parent Transaction entry is yielded,
    with the balance updated for just the postings that were in the list.
    (We attempt to preserve the original ordering of the postings as much as
    possible.)
    """

    # The running balance.
    balance = Inventory()

    # Previous date.
    prev_date = None

    # A list of entries at the current date.
    date_entries = []

    first = lambda pair: pair[0]
    for entry in entries:

        # Get the posting if we are dealing with one.
        if isinstance(entry, Posting):
            posting = entry
            entry = posting.entry
        else:
            posting = None

        if entry.date != prev_date:
            prev_date = entry.date

            # Flush the dated entries.
            for date_entry, date_postings in date_entries:
                if date_postings:
                    # Compute the change due to this transaction and update the
                    # total balance at the same time.
                    change = Inventory()
                    for date_posting in date_postings:
                        change.add_position(date_posting.position, True)
                        balance.add_position(date_posting.position, True)
                else:
                    change = None
                yield date_entry, date_postings, change, balance

            date_entries.clear()
            assert not date_entries

        if posting is not None:
            # De-dup multiple postings on the same transaction entry by
            # grouping their positions together.
            index = index_key(date_entries, entry, key=first)
            if index is None:
                date_entries.append( (entry, [posting]) )
            else:
                # We are indeed de-duping!
                postings = date_entries[index][1]
                postings.append(posting)
        else:
            # This is a regular entry; nothing to add/remove.
            date_entries.append( (entry, None) )

    # Flush the final dated entries if any, same as above.
    for date_entry, date_postings in date_entries:
        if date_postings:
            change = Inventory()
            for date_posting in date_postings:
                change.add_position(date_posting.position, True)
                balance.add_position(date_posting.position, True)
        else:
            change = None
        yield date_entry, date_postings, change, balance
    date_entries.clear()








FLAG_ROWTYPES = {
    data.FLAG_PADDING  : 'Padding',
    data.FLAG_SUMMARIZE: 'Summarize',
    data.FLAG_TRANSFER : 'Transfer',
}

def balance_html(balance):
    return ('\n<br/>'.join(map(str, balance.get_positions()))
            if balance
            else '')

def entries_table_with_balance(oss, account_postings, render_postings=True):
    """Render a list of entries into an HTML table.
    """
    write = lambda data: (oss.write(data), oss.write('\n'))

    write('''
      <table class="entry-table">
      <thead>
        <tr>
         <th class="datecell">Date</th>
         <th class="flag">F</th>
         <th class="description">Narration/Payee</th>
         <th class="position">Position</th>
         <th class="price">Price</th>
         <th class="cost">Cost</th>
         <th class="change">Change</th>
         <th class="balance">Balance</th>
      </thead>
    ''')

    balance = Inventory()
    for entry, leg_postings, change, balance in iterate_with_balance(account_postings):

        # Prepare the data to be rendered for this row.
        date = entry.date
        balance_str = balance_html(balance)

        rowtype = entry.__class__.__name__
        flag = ''
        extra_class = ''

        if isinstance(entry, Transaction):
            rowtype = FLAG_ROWTYPES.get(entry.flag, 'Transaction')
            extra_class = 'warning' if entry.flag == data.FLAG_WARNING else ''
            flag = entry.flag
            description = '<span class="narration">{}</span>'.format(entry.narration)
            if entry.payee:
                description = '<span class="payee">{}</span><span class="pnsep">|</span>{}'.format(entry.payee, description)
            change_str = balance_html(change)

        elif isinstance(entry, Check):
            # Check the balance here and possibly change the rowtype
            if not entry.success:
                rowtype = 'CheckFail'

            description = 'Check {} has {}'.format(account_link(entry.account), entry.position)
            change_str = str(entry.position)

        elif isinstance(entry, (Open, Close)):
            description = '{} {}'.format(entry.__class__.__name__, account_link(entry.account))
            change_str = ''

        else:
            description = entry.__class__.__name__
            change_str = ''

        # Render a row.
        write('''
          <tr class="{} {}">
            <td class="datecell">{}</td>
            <td class="flag">{}</td>
            <td class="description" colspan="4">{}</td>
            <td class="change num">{}</td>
            <td class="balance num">{}</td>
          <tr>
        '''.format(rowtype, extra_class,
                   date, flag, description, change_str, balance_str))

        if render_postings and isinstance(entry, Transaction):
            for posting in entry.postings:

                classes = ['Posting']
                if posting.flag == data.FLAG_WARNING:
                    classes.append('warning')
                if posting in leg_postings:
                    classes.append('leg')

                write('''
                  <tr class="{}">
                    <td class="datecell"></td>
                    <td class="flag">{}</td>
                    <td class="description">{}</td>
                    <td class="position num">{}</td>
                    <td class="price num">{}</td>
                    <td class="cost num">{}</td>
                    <td class="change num"></td>
                    <td class="balance num"></td>
                  <tr>
                '''.format(' '.join(classes),
                           posting.flag or '',
                           account_link(posting.account),
                           posting.position,
                           posting.price or '',
                           get_balance_amount(posting)))

    write('</table>')


def entries_table(oss, account_postings, render_postings=True):
    """Render a list of entries into an HTML table.
    """
    write = lambda data: (oss.write(data), oss.write('\n'))

    write('''
      <table class="entry-table">
      <thead>
        <tr>
         <th class="datecell">Date</th>
         <th class="flag">F</th>
         <th class="description">Narration/Payee</th>
         <th class="amount">Amount</th>
         <th class="cost">Cost</th>
         <th class="price">Price</th>
         <th class="balance">Balance</th>
      </thead>
    ''')

    balance = Inventory()
    for entry, leg_postings, change, balance in iterate_with_balance(account_postings):

        # Prepare the data to be rendered for this row.
        date = entry.date
        rowtype = entry.__class__.__name__
        flag = ''
        extra_class = ''

        if isinstance(entry, Transaction):
            rowtype = FLAG_ROWTYPES.get(entry.flag, 'Transaction')
            extra_class = 'warning' if entry.flag == data.FLAG_WARNING else ''
            flag = entry.flag
            description = '<span class="narration">{}</span>'.format(entry.narration)
            if entry.payee:
                description = '<span class="payee">{}</span><span class="pnsep">|</span>{}'.format(entry.payee, description)
            change_str = balance_html(change)

        elif isinstance(entry, Check):
            # Check the balance here and possibly change the rowtype
            if not entry.success:
                rowtype = 'CheckFail'

            description = 'Check {} has {}'.format(account_link(entry.account), entry.position)

        elif isinstance(entry, (Open, Close)):
            description = '{} {}'.format(entry.__class__.__name__, account_link(entry.account))

        else:
            description = entry.__class__.__name__

        # Render a row.
        write('''
          <tr class="{} {}">
            <td class="datecell">{}</td>
            <td class="flag">{}</td>
            <td class="description" colspan="5">{}</td>
          <tr>
        '''.format(rowtype, extra_class,
                   date, flag, description))

        if render_postings and isinstance(entry, Transaction):
            for posting in entry.postings:

                classes = ['Posting']
                if posting.flag == data.FLAG_WARNING:
                    classes.append('warning')

                write('''
                  <tr class="{}">
                    <td class="datecell"></td>
                    <td class="flag">{}</td>
                    <td class="description">{}</td>
                    <td class="amount num">{}</td>
                    <td class="cost num">{}</td>
                    <td class="price num">{}</td>
                    <td class="balance num">{}</td>
                  <tr>
                '''.format(' '.join(classes),
                           posting.flag or '',
                           account_link(posting.account),
                           posting.position.get_amount(),
                           posting.position.lot.cost or '',
                           posting.price or '',
                           get_balance_amount(posting)))

    write('</table>')


@viewapp.route('/journal', name='journal')
def journal():
    "A list of all the entries in this realization."
    bottle.redirect(app_url('account', slashed_account_name=''))


@viewapp.route('/account/<slashed_account_name:re:[^:]*>', name='account')
def account(slashed_account_name=None):
    "A list of all the entries for this account realization."

    # Get the appropriate realization: if we're looking at the balance sheet, we
    # want to include the net-income transferred from the exercise period.
    account_name = slashed_account_name.strip('/').replace('/', ':')
    options = app.contents.options
    if data.is_balance_sheet_account_name(account_name, options):
        real_accounts = request.view.closing_real_accounts
    else:
        real_accounts = request.view.real_accounts

    account_postings = realization.get_subpostings(real_accounts[account_name])

    oss = io.StringIO()
    entries_table_with_balance(oss, account_postings)
    return render_app(
        pagetitle = '{}'.format(account_name), # Account:
        contents = oss.getvalue())


def get_conversion_entries(entries):
    """Return the subset of transaction entries which have a conversion."""
    return [entry
            for entry in utils.filter_type(entries, Transaction)
            if data.transaction_has_conversion(entry)]


@viewapp.route('/conversions', name='conversions')
def conversions():
    "Render the list of transactions with conversions."

    view = request.view

    oss = io.StringIO()
    conversion_entries = get_conversion_entries(view.entries)
    entries_table(oss, conversion_entries, render_postings=True)

    balance = realization.compute_total_balance(conversion_entries)

    return render_app(
        pagetitle = "Conversions",
        contents = """
          <div id="table">
            {}
          </div>
          <h3>Conversion Total:<span class="num">{}</span></h3>
        """.format(oss.getvalue(), balance))












@viewapp.route('/positions', name='positions')
def positions():
    "Render information about positions at the end of all entries."

    entries = request.view.entries

    total_balance = realization.compute_total_balance(entries)

    # FIXME: Make this into a nice table.

    # FIXME: Group the positions by currency, show the price of each position in
    # the inventory (see year 2006 for a good sample input).

    oss = io.StringIO()
    for position in total_balance.get_positions():
        if position.lot.cost or position.lot.lot_date:
            cost = position.get_cost()

            ## FIXME: remove
            # print(('{p.number:12.2f} {p.lot.currency:8} '
            #        '{p.lot.cost.number:12.2f} {p.lot.cost.currency:8} '
            #        '{c.number:12.2f} {c.currency:8}').format(p=position, c=cost))
            # rows.append((position.lot.currency, position.lot.cost.currency,
            #              position.number, position.lot.cost.number, cost.number))

            oss.write('''
              <div class="position num">
                 {position}     {cost}
              </div>
            '''.format(position=position, cost=position.get_cost()))


    if 0:
        # Manipulate it a bit with Pandas.
        import pandas
        import numpy
        df = pandas.DataFrame(rows,
                              columns=['ccy', 'cost_ccy', 'units', 'unit_cost', 'total_cost'])

        # print(df.to_string())

        sums = df.groupby(['ccy', 'cost_ccy']).sum()

        total_cost = sums['total_cost'].astype(float)
        sums['percent'] = 100 * total_cost / total_cost.sum()

        sums.insert(2, 'average_cost', total_cost / sums['units'].astype(float))

    return render_app(
        pagetitle = "Positions",
        contents = oss.getvalue()
        )


@viewapp.route('/trades', name='trades')
def trades():
    "Render a list of the transactions booked against inventory-at-cost."
    return render_app(
        pagetitle = "Trades",
        contents = ""
        )


@viewapp.route('/documents', name='documents')
def documents():
    "Render a tree with the documents found for each."
    return render_app(
        pagetitle = "Documents",
        contents = ""
        )




#--------------------------------------------------------------------------------
# Views.


# A cache for views that have been created (on access).
app.views = {}


class View:
    """A container for filtering a subset of entries and realizing that for
    display."""

    def __init__(self, all_entries, options, title):

        # A reference to the full list of padded entries.
        self.all_entries = all_entries

        # List of filterered entries for this view, and index at the beginning
        # of the period transactions, past the opening balances. These are
        # computed in _realize().
        self.entries = None
        self.opening_entries = None
        self.closing_entries = None

        # Title.
        self.title = title

        # A reference to the global list of options and the account type names.
        self.options = options
        self.account_types = get_account_types(options)

        # Realization of the filtered entries to display. These are computed in
        # _realize().
        self.real_accounts = None
        self.opening_real_accounts = None
        self.closing_real_accounts = None

        # Realize now, we don't need to do this lazily because we create these
        # view objects on-demand and cache them.
        self._realize()

    def _realize(self):
        """Compute the list of filtered entries and transaction tree."""

        # Get the filtered list of entries.
        self.entries, self.begin_index = self.apply_filter(self.all_entries, self.options)

        # Compute the list of entries for the opening balances sheet.
        self.opening_entries = (self.entries[:self.begin_index]
                                if self.begin_index is not None
                                else None)


        # Compute the list of entries that includes transfer entries of the
        # income/expenses amounts to the balance sheet's equity (as "net
        # income"). This is used to render the end-period balance sheet, with
        # the current period's net income, closing the period.
        equity = self.options['name_equity']
        account_netincome = '{}:{}'.format(equity, self.options['account_netincome'])
        account_netincome = data.Account(account_netincome,
                                         data.account_type(account_netincome))

        self.closing_entries = summarize.transfer(self.entries, None,
                                                  data.is_income_statement_account, account_netincome)

        # Realize the three sets of entries.
        do_check = False
        if self.opening_entries:
            with utils.print_time('realize_opening'):
                self.opening_real_accounts = realization.realize(self.opening_entries, do_check, self.account_types)
        else:
            self.opening_real_accounts = None

        with utils.print_time('realize'):
            self.real_accounts = realization.realize(self.entries, do_check, self.account_types)

        with utils.print_time('realize_closing'):
            self.closing_real_accounts = realization.realize(self.closing_entries, do_check, self.account_types)

        assert self.real_accounts is not None
        assert self.closing_real_accounts is not None

    def apply_filter(self, entries):
        "Filter the list of entries."
        raise NotImplementedError



class AllView(View):

    def apply_filter(self, entries, options):
        "Return the list of entries unmodified."
        return (entries, None)

@app.route(r'/view/all/<path:re:.*>', name='all')
@handle_view(2)
def all(path=None):
    return AllView(contents.entries, contents.options, 'All Transactions')



class YearView(View):

    def __init__(self, entries, options, title, year):
        self.year = year
        View.__init__(self, entries, options, title)

    def apply_filter(self, entries, options):
        "Return entries for only that year."

        # Get the transfer account objects.
        #
        # FIXME: We should probably create these globally and then all fetch the
        # same instances.
        equity = options['name_equity']
        account_earnings = '{}:{}'.format(equity, options['account_earnings'])
        account_earnings = data.Account(account_earnings, data.account_type(account_earnings))

        account_opening = '{}:{}'.format(equity, options['account_opening'])
        account_opening = data.Account(account_opening, data.account_type(account_opening))

        # Clamp to the desired period.
        begin_date = datetime.date(self.year, 1, 1)
        end_date = datetime.date(self.year+1, 1, 1)
        with utils.print_time('clamp'):
            entries, index = summarize.clamp(entries,
                                             begin_date, end_date,
                                             account_earnings, account_opening)

        return entries, index

@app.route(r'/view/year/<year:re:\d\d\d\d>/<path:re:.*>', name='year')
@handle_view(3)
def year(year=None, path=None):
    year = int(year)
    return YearView(contents.entries, contents.options, 'Year {:4d}'.format(year), year)



class TagView(View):

    def __init__(self, entries, options, title, tags):
        # The tags we want to include.
        assert isinstance(tags, (set, list, tuple))
        self.tags = tags

        View.__init__(self, entries, options, title)

    def apply_filter(self, entries, options):
        "Return only entries with the given tag."

        tags = self.tags
        tagged_entries = [entry
                          for entry in entries
                          if isinstance(entry, data.Transaction) and (entry.tags & tags)]

        return tagged_entries, None

@app.route(r'/view/tag/<tag:re:\d\d\d\d>/<path:re:.*>', name='tag')
@handle_view(3)
def tag(tag=None, path=None):
    return TagView(contents.entries, contents.options, 'Tag {:4d}'.format(tag), tag)



class PayeeView(View):

    def __init__(self, entries, options, title, payee):
        # The payee to filter.
        assert isinstance(payee, str)
        self.payee = payee

        View.__init__(self, entries, options, title)

    def apply_filter(self, entries, options):
        "Return only transactions for the given payee."

        payee = self.payee
        payee_entries = [entry
                         for entry in entries
                         if isinstance(entry, data.Transaction) and (entry.payee == payee)]

        return payee_entries, None

@app.route(r'/view/payee/<payee:re:\d\d\d\d>/<path:re:.*>', name='payee')
@handle_view(3)
def payee(payee=None, path=None):
    return PayeeView(contents.entries, contents.options, 'Payee {:4d}'.format(payee), payee)



#--------------------------------------------------------------------------------
# Bootstrapping and main program.


# A global list of all available ledgers (apps).
VIEWS = []


def app_mount(view):
    "Create and mount a new app for a view."

    # Create and customize the new app.
    app_copy = copy.copy(app)
    app_copy.view = view

    # Mount it on the root application.
    bottle.mount('/view/{}'.format(view.id), app_copy, name=view.id)

    # Update the global list of ledgers.
    VIEWS.append(view)


def create_realizations(entries, options):
    """Create apps for all the realizations we want to be able to render."""

    # The global realization, with all entries.
    app_mount(AllView(entries, options,
                      'all', 'All Transactions'))

    # One realization by-year.
    for year in reversed(list(data.get_active_years(entries))):
        view = YearView(entries, options,
                        'year{:4d}'.format(year), 'Year {:4d}'.format(year), year)
        app_mount(view)

    # Create views for all tags.
    for tagid, tag in compute_ids(get_all_tags(entries)):
        view = TagView(entries, options, tagid, 'Tag "{}"'.format(tag), {tag})
        app_mount(view)

    # FIXME: We need to make the payee mount different and "dynamic", createing
    # a new view automatically. We should do the same for the years and tags as
    # well. Creating the mounts is too expensive; views need to be created
    # on-demand, we need a special mount.
    if 0:
        # Create views for all payees.
        for payeeid, payee in compute_ids(get_all_payees(entries)):
            view = PayeeView(entries, options, payeeid, 'Payee "{}"'.format(payee), payee)
            app_mount(view)


def load_input_file(filename):
    """Parse the input file, pad the entries and validate it.
    This also prints out the error messages."""

    # Parse the input file.
    with utils.print_time('parse'):
        contents = parser.parse(filename)
        parse_errors = contents.parse_errors
        data.print_errors(contents.parse_errors)

    # Pad the resulting entries (create synthetic Pad entries to balance checks
    # where desired).
    with utils.print_time('pad'):
        entries, pad_errors = realization.pad(contents.entries)
        data.print_errors(pad_errors)

    with utils.print_time('check'):
        entries, check_errors = realization.check(entries)
        data.print_errors(check_errors)

    # Validate the list of entries.
    with utils.print_time('validation'):
        valid_errors = validation.validate(entries, contents.accounts)
        data.print_errors(valid_errors)

    return contents, entries



## FIXME: remove
# def stopwatch(callback):
#     def wrapper(*args, **kwargs):
#         start = time.time()
#         body = callback(*args, **kwargs)
#         end = time.time()
#         response.headers['X-Exec-Time'] = str(end - start)
#         print(str(end - start))
#         return body
#     return wrapper



def main():
    argparser = argparse.ArgumentParser(__doc__.strip())
    argparser.add_argument('filename', help="Beancount input filename to serve.")
    argparser.add_argument('--debug', action='store_true',
                           help="Enable debugging features (auto-reloading of css).")
    args = argparser.parse_args()

    # Parse the beancount file.
    #
    ## FIXME: maybe we can do away with this, and attach it to
    ## the global application class.
    global contents, clean_entries
    contents, clean_entries = load_input_file(args.filename)

    ## FIXME: Not sure what to do with errors yet.

    # Save globals in the global app.
    global app
    app.args = args
    app.contents = contents
    app.entries = clean_entries

    # Load templates.
    with open(path.join(path.dirname(__file__), 'template.html')) as f:
        global template
        template = bottle.SimpleTemplate(f)

    with open(path.join(path.dirname(__file__), 'style.css')) as f:
        global STYLE; STYLE = f.read()

    # # Create all the basic realizations.
    # create_realizations(clean_entries, contents.options)

    # Run the server.
    app.run(host='localhost', port=8080, debug=args.debug) # reloader=True









# FIXME: move this to data.py.

def get_all_tags(entries):
    "Return a list of all the tags seen in the given entries."
    all_tags = set()
    for entry in utils.filter_type(entries, data.Transaction):
        all_tags.update(entry.tags)
    return all_tags


def get_all_payees(entries):
    "Return a list of all the unique payees seen in the given entries."
    all_payees = set()
    for entry in utils.filter_type(entries, data.Transaction):
        all_payees.add(entry.payee)
    all_payees.discard(None)
    return all_payees


def compute_ids(strings):
    """Given a sequence of strings, reduce them to corresponding ids without any
    funny characters and insure that the list of ids is unique. Yields pairs
    of (id, string) for the result."""

    string_set = set(strings)

    # Try multiple methods until we get one that has no collisions.
    for regexp, replacement in [('[^A-Za-z0-9-.]', '_'),
                                ('[^A-Za-z0-9]', ''),]:

        # Map ids to strings.
        idmap = defaultdict(list)
        for string in string_set:
            id = re.sub(regexp, replacement, string)
            idmap[id].append(string)

        # Check for collisions.
        if all(len(stringlist) == 1 for stringlist in idmap.values()):
            break
    else:
        raise RuntimeError("Could not find a unique mapping for {}".format(string_set))

    return sorted((id, stringlist[0]) for id, stringlist in idmap.items())








# def app_url(global_name, global_kwargs, name, kwargs=None):
#     if global_kwargs is None:
#         global_kwargs = {}
#     if kwargs is None:
#         kwargs = {}
#     view_url = viewapp.router.build(name, **kwargs)
#     view_url = view_url.lstrip('/')
#     return app.router.build(global_name, view_url, **global_kwargs)