import sys
import tkinter as tk
import vlc


def play_media(url: str) -> None:
    root = tk.Tk()
    root.attributes('-fullscreen', True)
    root.configure(background='black')
    frame = tk.Frame(root, background='black')
    frame.pack(fill=tk.BOTH, expand=True)

    instance = vlc.Instance()
    player = instance.media_player_new()
    media = instance.media_new(url)
    player.set_media(media)

    handle = frame.winfo_id()
    if sys.platform.startswith('win'):
        player.set_hwnd(handle)
    else:
        player.set_xwindow(handle)

    player.play()
    root.mainloop()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python vlc_embed.py <media_url>')
        sys.exit(1)
    play_media(sys.argv[1])
