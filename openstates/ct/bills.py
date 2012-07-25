import re
import datetime
from operator import itemgetter
from collections import defaultdict

from billy.scrape import NoDataForPeriod
from billy.scrape.bills import BillScraper, Bill
from billy.scrape.votes import Vote
from .utils import parse_directory_listing, open_csv

import lxml.html


class CTBillScraper(BillScraper):
    state = 'ct'
    latest_only = True

    def scrape(self, session, chambers):
        self.bills = {}
        self._committee_names = {}
        self._introducers = defaultdict(set)
        self._subjects = defaultdict(list)

        self.scrape_committee_names()
        self.scrape_subjects()
        self.scrape_introducers('upper')
        self.scrape_introducers('lower')
        self.scrape_bill_info(session, chambers)
        for chamber in chambers:
            self.scrape_versions(chamber, session)
        self.scrape_bill_history()

        for bill in self.bills.itervalues():
            self.save_bill(bill)

    def scrape_bill_info(self, session, chambers):
        info_url = "ftp://ftp.cga.ct.gov/pub/data/bill_info.csv"
        data = self.urlopen(info_url)
        page = open_csv(data)

        chamber_map = {'H': 'lower', 'S': 'upper'}

        for row in page:
            bill_id = row['bill_num']
            chamber = chamber_map[bill_id[0]]

            if not chamber in chambers:
                continue

            # assert that the bill data is from this session, CT is tricky
            assert row['sess_year'] == session

            if re.match(r'^(S|H)J', bill_id):
                bill_type = 'joint resolution'
            elif re.match(r'^(S|H)R', bill_id):
                bill_type = 'resolution'
            else:
                bill_type = 'bill'

            bill = Bill(session, chamber, bill_id,
                        row['bill_title'],
                        type=bill_type)
            bill.add_source(info_url)

            self.scrape_bill_page(bill)

            for introducer in self._introducers[bill_id]:
                bill.add_sponsor('primary', introducer,
                                 official_type='introducer')

            bill['subjects'] = self._subjects[bill_id]

            self.bills[bill_id] = bill

    def scrape_bill_page(self, bill):
        url = ("http://www.cga.ct.gov/asp/cgabillstatus/"
               "cgabillstatus.asp?selBillType=Bill"
               "&bill_num=%s&which_year=%s" % (bill['bill_id'],
                                               bill['session']))
        with self.urlopen(url) as page:
            page = lxml.html.fromstring(page)
            page.make_links_absolute(url)
            bill.add_source(url)

            if not bill['sponsors']:
                intro_com = page.xpath('//td[contains(string(), "Introduced by:")]')[1].text_content().replace('Introduced by:', '').strip()
                bill.add_sponsor('introducer', intro_com)

            for link in page.xpath("//a[contains(@href, '/FN/')]"):
                bill.add_document(link.text.strip(), link.attrib['href'])

            for link in page.xpath("//a[contains(@href, '/BA/')]"):
                bill.add_document(link.text.strip(), link.attrib['href'])

            for link in page.xpath("//a[contains(@href, 'VOTE')]"):
                # 2011 HJ 31 has a blank vote, others might too
                if link.text:
                    self.scrape_vote(bill, link.text.strip(),
                                     link.attrib['href'])

    def scrape_vote(self, bill, name, url):
        if "VOTE/H" in url:
            vote_chamber = 'lower'
            cols = (1, 5, 9, 13)
            name_offset = 3
            yes_offset = 0
            no_offset = 1
        else:
            vote_chamber = 'upper'
            cols = (1, 6)
            name_offset = 4
            yes_offset = 1
            no_offset = 2

        with self.urlopen(url) as page:
            if 'BUDGET ADDRESS' in page:
                return

            page = lxml.html.fromstring(page)

            yes_count = page.xpath(
                "string(//span[contains(., 'Those voting Yea')])")
            yes_count = int(re.match(r'[^\d]*(\d+)[^\d]*', yes_count).group(1))

            no_count = page.xpath(
                "string(//span[contains(., 'Those voting Nay')])")
            no_count = int(re.match(r'[^\d]*(\d+)[^\d]*', no_count).group(1))

            other_count = page.xpath(
                "string(//span[contains(., 'Those absent')])")
            other_count = int(
                re.match(r'[^\d]*(\d+)[^\d]*', other_count).group(1))

            need_count = page.xpath(
                "string(//span[contains(., 'Necessary for')])")
            need_count = int(
                re.match(r'[^\d]*(\d+)[^\d]*', need_count).group(1))

            date = page.xpath("string(//span[contains(., 'Taken on')])")
            date = re.match(r'.*Taken\s+on\s+(\d+/\s?\d+)', date).group(1)
            date = date.replace(' ', '')
            date = datetime.datetime.strptime(date + " " + bill['session'],
                                              "%m/%d %Y").date()

            vote = Vote(vote_chamber, date, name, yes_count > need_count,
                        yes_count, no_count, other_count)
            vote.add_source(url)

            table = page.xpath("//table")[0]
            for row in table.xpath("tr"):
                for i in cols:
                    name = row.xpath("string(td[%d])" % (
                        i + name_offset)).strip()

                    if not name or name == 'VACANT':
                        continue

                    if "Y" in row.xpath("string(td[%d])" %
                                        (i + yes_offset)):
                        vote.yes(name)
                    elif "N" in row.xpath("string(td[%d])" %
                                          (i + no_offset)):
                        vote.no(name)
                    else:
                        vote.other(name)

            bill.add_vote(vote)


    def scrape_subjects(self):
        for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            url = ('http://www.cga.ct.gov/asp/cgasubjectsearch/'
                   'default.asp?LeadingChar=' + letter)
            html = self.urlopen(url)
            doc = lxml.html.fromstring(html)
            doc.make_links_absolute(url)

            for subj in doc.xpath('//a[contains(@href, "subbills")]'):
                subj_html = self.urlopen(subj.get('href'))
                for bill_id in doc.xpath('//a[contains(@href, "CGABillStatus")]/text()'):
                    self._subjects[bill_id].append(subj.text)


    def scrape_bill_history(self):
        history_url = "ftp://ftp.cga.ct.gov/pub/data/bill_history.csv"
        page = self.urlopen(history_url)
        page = open_csv(page)

        action_rows = defaultdict(list)

        for row in page:
            bill_id = row['bill_num']

            if bill_id in self.bills:
                action_rows[bill_id].append(row)

        for (bill_id, actions) in action_rows.iteritems():
            bill = self.bills[bill_id]

            actions.sort(key=itemgetter('act_date'))
            act_chamber = bill['chamber']

            for row in actions:
                date = row['act_date']
                date = datetime.datetime.strptime(
                    date, "%Y-%m-%d %H:%M:%S").date()

                action = row['act_desc'].strip()
                act_type = []

                match = re.search('COMM(ITTEE|\.) ON$', action)
                if match:
                    comm_code = row['qual1']
                    comm_name = self._committee_names.get(comm_code,
                                                          comm_code)
                    action = "%s %s" % (action, comm_name)
                    act_type.append('committee:referred')
                elif row['qual1']:
                    if bill['session'] in row['qual1']:
                        action += ' (%s' % row['qual1']
                        if row['qual2']:
                            action += ' %s)' % row['qual2']
                    else:
                        action += ' %s' % row['qual1']

                match = re.search(r'REFERRED TO OLR, OFA (.*)',
                                  action)
                if match:
                    action = ('REFERRED TO Office of Legislative Research'
                              ' AND Office of Fiscal Analysis %s' % (
                                  match.group(1)))

                if (re.match(r'^ADOPTED, (HOUSE|SENATE)', action) or
                    re.match(r'^(HOUSE|SENATE) PASSED', action)):
                    act_type.append('bill:passed')

                match = re.match(r'^Joint ((Un)?[Ff]avorable)', action)
                if match:
                    act_type.append('committee:passed:%s' %
                                    match.group(1).lower())

                if not act_type:
                    act_type = ['other']

                bill.add_action(act_chamber, action, date,
                                type=act_type)

                if 'TRANS.TO HOUSE' in action or action == 'SENATE PASSED':
                    act_chamber = 'lower'

                if ('TRANSMITTED TO SENATE' in action or
                    action == 'HOUSE PASSED'):
                    act_chamber = 'upper'

    def scrape_versions(self, chamber, session):
        chamber_letter = {'upper': 's', 'lower': 'h'}[chamber]
        versions_url = "ftp://ftp.cga.ct.gov/%s/tob/%s/" % (
            session, chamber_letter)

        with self.urlopen(versions_url) as page:
            files = parse_directory_listing(page)

            for f in files:
                match = re.match(r'^\d{4,4}([A-Z]+-\d{5,5})-(R\d\d)',
                                 f.filename)
                bill_id = match.group(1).replace('-', '')

                try:
                    bill = self.bills[bill_id]
                except KeyError:
                    continue

                url = versions_url + f.filename
                bill.add_version(match.group(2), url)

    def scrape_committee_names(self):
        comm_url = "ftp://ftp.cga.ct.gov/pub/data/committee.csv"
        page = self.urlopen(comm_url)
        page = open_csv(page)

        for row in page:
            comm_code = row['comm_code'].strip()
            comm_name = row['comm_name'].strip()
            comm_name = re.sub(r' Committee$', '', comm_name)
            self._committee_names[comm_code] = comm_name

    def scrape_introducers(self, chamber):
        chamber_letter = {'upper': 's', 'lower': 'h'}[chamber]
        url = "http://www.cga.ct.gov/asp/menu/%slist.asp" % chamber_letter

        with self.urlopen(url) as page:
            page = lxml.html.fromstring(page)
            page.make_links_absolute(url)

            for link in page.xpath("//a[contains(@href, 'MemberBills')]"):
                name = link.xpath("string(../../td[1])").strip()
                name = re.match("^S?\d+\s+-\s+(.*)$", name).group(1)

                self.scrape_introducer(name, link.attrib['href'])

    def scrape_introducer(self, name, url):
        with self.urlopen(url) as page:
            page = lxml.html.fromstring(page)

            for link in page.xpath("//a[contains(@href, 'billstatus')]"):
                bill_id = link.text.strip()
                self._introducers[bill_id].add(name)
