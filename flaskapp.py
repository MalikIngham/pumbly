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
    return render_template('index.html')

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

    #General blog information. Here I'm going to get the max posts from the specified blog
    blog_name = request.form.get('blog_name')
    # return render_template('search.html', blog_name=blog_name)
    # print('blog name: ', blog_name)

    blog = client.blog_info(blog_name)
    # print('blog: ', blog)
    max_posts = blog['blog']['posts']
    
    list_of_posts = []

    offset = 0
    count = 0
    # print(max_posts)
    while max_posts > offset:
        count += 1
        # print(count)
        if count % 10 == 0:
            print('sleeping dont bother me')
            time.sleep(60)

        # print('Current offset: ',offset)
        posts = client.posts(blog_name, reblog_info=False, notes_info=False, \
            limit=10, offset=offset)
        try:
            for item in posts.get('posts'):
                try:
                    trail = item.get('trail')[0]
                    if trail['blog'].get('name') == blog_name:
                        list_of_posts.append(item)
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

        # response = {
        #     'photo': posts['image'],
        #     'notes': posts['notes'],
        #     'url': posts['url']
        # }
    # print('list of posts: ', list_of_posts)
    print(list_of_posts[0])


    # print(list_of_posts)
    # print('outside for loop')
    # for post in list_of_posts:
    #     print(post['post_url'])
    # print(list_of_posts)

@app.route('/results')
def results(list_of_posts=None):
    '''Page that displays all the original posts correctly. '''

    if list_of_posts:
        return render_template('posts.html')
    else:
        return render_template('error.html')




#not used at the moment. I'll circle back to this asap 
def get_all_posts(max_posts=None, current_offset=None, blog_name=None):
    '''Recursive function call to iterate through all posts '''

    client = TumblrRestClient(
        f'{consumer_key}',
        f'{consumer_secret_key}',
        f'{oath_token}',
        f'{oath_secret}'
    )
    
    client.posts('sazzmine', reblog_info=False, notes_info=False, limit=50, offset=current_offset)

    if current_offset >= max_posts:
        return False

    else:
        if current_offset + 50 > max_posts:
            return get_all_posts(max_posts, current_offset + (max_posts-current_offset))
        return get_all_posts(max_posts, current_offset+50)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=2000)