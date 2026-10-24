"""Windows desktop balloon notifications through the native shell API."""
import ctypes
import os
from ctypes import wintypes

class GUID(ctypes.Structure):
    _fields_=[('Data1',wintypes.DWORD),('Data2',wintypes.WORD),('Data3',wintypes.WORD),('Data4',ctypes.c_ubyte*8)]

class NOTIFYICONDATA(ctypes.Structure):
    _fields_=[('cbSize',wintypes.DWORD),('hWnd',wintypes.HWND),('uID',wintypes.UINT),
        ('uFlags',wintypes.UINT),('uCallbackMessage',wintypes.UINT),('hIcon',wintypes.HANDLE),
        ('szTip',wintypes.WCHAR*128),('dwState',wintypes.DWORD),('dwStateMask',wintypes.DWORD),
        ('szInfo',wintypes.WCHAR*256),('uTimeoutOrVersion',wintypes.UINT),('szInfoTitle',wintypes.WCHAR*64),
        ('dwInfoFlags',wintypes.DWORD),('guidItem',GUID),('hBalloonIcon',wintypes.HANDLE)]

class DesktopNotifier:
    def __init__(self,hwnd,icon):
        if os.name!='nt':raise OSError('此版本的桌面通知仅支持 Windows')
        self.shell=ctypes.windll.shell32.Shell_NotifyIconW
        self.shell.argtypes=[wintypes.DWORD,ctypes.POINTER(NOTIFYICONDATA)];self.shell.restype=wintypes.BOOL
        loader=ctypes.windll.user32.LoadImageW
        loader.argtypes=[wintypes.HINSTANCE,wintypes.LPCWSTR,wintypes.UINT,ctypes.c_int,ctypes.c_int,wintypes.UINT]
        loader.restype=wintypes.HANDLE
        self.icon=loader(None,str(icon),1,0,0,0x10|0x40)
        self.data=NOTIFYICONDATA();self.data.cbSize=ctypes.sizeof(NOTIFYICONDATA)
        self.data.hWnd=hwnd;self.data.uID=127;self.data.uFlags=2|4
        self.data.hIcon=self.icon;self.data.szTip='Codex 监控台 · 完成通知'
        self.added=bool(self.shell(0,ctypes.byref(self.data)))

    def notify(self,title,message):
        if not self.added:return False
        self.data.uFlags=0x10;self.data.szInfoTitle=str(title)[:55];self.data.szInfo=str(message)[:220]
        self.data.uTimeoutOrVersion=10000;self.data.dwInfoFlags=1|0x80
        return bool(self.shell(1,ctypes.byref(self.data)))

    def close(self):
        if self.added:
            self.shell(2,ctypes.byref(self.data));self.added=False
        if self.icon:
            destroy=ctypes.windll.user32.DestroyIcon;destroy.argtypes=[wintypes.HANDLE]
            destroy(self.icon);self.icon=None
