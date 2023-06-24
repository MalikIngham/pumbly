import time
from flask import Flask, request, render_template
from pytumblr import TumblrRestClient

app = Flask(__name__)

app.config.from_pyfile('localflaskapp.cfg')

consumer_key = app.config.get('CONSUMER_KEY')
consumer_secret_key = app.config.get('CONSUMER_SECRET_KEY')
oath_token = app.config.get('OATH_TOKEN')
oath_secret = app.config.get('OATH_SECRET')


@app.route('/')
def index():
    '''Index function call. Only displaying a basic welcome page here'''
    
    client = TumblrRestClient(
        f'{consumer_key}',
        f'{consumer_secret_key}',
        f'{oath_token}',
        f'{oath_secret}'
    )

    blog_name = 'sazzmine'

    blog = client.blog_info('sazzmine')
    total_posts = blog['blog']['posts']
    original_posts = []

    offset = 0
    count = 0
    while total_posts>offset:
        count += 1
        if count % 50 == 0:
            time.sleep(30)
        
        blogs_content = client.posts(blog_name, reblog_info=False, notes_info=False, \
            limit=100, offset=offset)
        print(blogs_content)
        for posts in blogs_content.get('posts'):
            trail = posts.get('trail') if posts.get('trail') else None
            if trail:
                print('*'*30)
                print(trail)
                blog = trail[0] if trail[0] else None
                if blog:
                    print(blog['blog'])
                    if blog['blog']['name'] == blog_name:
                        original_posts.append(posts)
        
        if offset + 50 > total_posts:
            offset += (total_posts-offset)
        else:
            offset += 50

        if offset == total_posts:
            break

    # while max_posts > offset:
    #     count += 1
    #     if count % 10 == 0:
    #         print('sleeping dont bother me')
    #         time.sleep(60)

    #     posts = client.posts(blog_name, reblog_info=False, notes_info=False, \
    #         limit=10, offset=offset)
    #     print('*'*30)
    #     print(posts)
    #     try:
    #         for item in posts.get('posts'):
    #             try:
    #                 trail = item.get('trail')[0]
    #                 if trail['blog'].get('name') == blog_name:
    #                     original_posts.append(item)
    #             except TypeError:
    #                 pass
    #     except (IndexError, TypeError) as error:
    #         print(error)
    #     if offset + 50 > max_posts:
    #         offset += (max_posts-offset)
    #     else:
    #         offset += 50

    #     if offset == max_posts:
    #         break

    # print(original_posts[0])

    return original_posts
    # return render_template('index.html')

@app.route('/search', methods=['POST'])
def search():
    '''Search functionality to search through tumblr api'''
    print(request.form)
    
    client = TumblrRestClient(
        f'{consumer_key}',
        f'{consumer_secret_key}',
        f'{oath_token}',
        f'{oath_secret}'
    )

    blog_name = request.form.get('blog_name')

    blog = client.blog_info(blog_name)
    
    max_posts = blog['blog']['posts']
    
    original_posts = []

    offset = 0
    count = 0
    while max_posts > offset:
        count += 1
        if count % 10 == 0:
            print('sleeping dont bother me')
            time.sleep(60)

        posts = client.posts(blog_name, reblog_info=False, notes_info=False, \
            limit=10, offset=offset)
        try:
            for item in posts.get('posts'):
                try:
                    trail = item.get('trail')[0]
                    if trail['blog'].get('name') == blog_name:
                        original_posts.append(item)
                except TypeError:
                    pass
        
        except (IndexError, TypeError) as error:
            print(error)
        
        if offset + 50 > max_posts:
            offset += (max_posts-offset)
        else:
            offset += 50

        if offset == max_posts:
            break

    print(original_posts[0])
    return 'SEARCH PAGE'

# @app.route('/get-original-posts', methods=['POST'])


@app.route('/results')
def results(list_of_posts=None):
    '''Page that displays all the original posts correctly. '''

    if list_of_posts:
        return render_template('posts.html')
    else:
        return render_template('error.html')


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=2000)