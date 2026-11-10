import { BlogSearch } from "@/components/BlogSearch";
import { TumblrAccount } from "@/components/TumblrAccount";

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col items-center gap-10 px-4 py-16 md:py-24">
      <header className="text-center">
        <h1 className="mb-4 text-5xl font-bold md:text-6xl xl:text-7xl">
          Pumbly
        </h1>
        <p>View the original posts from any Tumblr blog</p>
      </header>
      <TumblrAccount />
      <BlogSearch />
    </main>
  );
}
