import requests
from pprint import pprint

"""
Test client for calling the proxy server
"""

proxies = {
    'http': 'http://127.0.0.1:8000',
}

res = requests.get('http://pyback-lb-197482116.us-east-2.elb.amazonaws.com/',
                   proxies=proxies)
pprint(res.content)
