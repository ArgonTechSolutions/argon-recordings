/* Fence ordinary FFmpeg/libavformat URL connections; not a native-code sandbox. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netdb.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
static unsigned port;
static int (*real_connect)(int,const struct sockaddr*,socklen_t);
static int valid_port(const char *s) {
    if (!s || !*s) return 0;
    unsigned n=0; for (;*s;s++) { if (*s<'0'||*s>'9') return 0; n=n*10+(*s-'0'); if(n>65535)return 0; }
    return n>=1024 ? (int)n : 0;
}
int connect(int fd,const struct sockaddr *a,socklen_t len) {
    if(!real_connect) real_connect=dlsym(RTLD_NEXT,"connect");
    if(!a || len<sizeof(struct sockaddr_in) || a->sa_family!=AF_INET) {errno=EACCES;return -1;}
    const struct sockaddr_in *v=(const struct sockaddr_in*)a;
    if(v->sin_addr.s_addr!=htonl(INADDR_LOOPBACK) || ntohs(v->sin_port)!=port) {errno=EACCES;return -1;}
    return real_connect(fd,a,len);
}
int getaddrinfo(const char *node,const char *service,const struct addrinfo *h,struct addrinfo **r) {
    if(!node || strcmp(node,"127.0.0.1") || valid_port(service)!=(int)port) return EAI_NONAME;
    int (*f)(const char*,const char*,const struct addrinfo*,struct addrinfo**)=dlsym(RTLD_NEXT,"getaddrinfo");
    return f(node,service,h,r);
}
struct hostent *gethostbyname(const char *name) { h_errno=HOST_NOT_FOUND;return NULL; }
struct hostent *gethostbyname2(const char *name,int af) { h_errno=HOST_NOT_FOUND;return NULL; }
__attribute__((constructor)) static void attest(void) {
    port=(unsigned)valid_port(getenv("RECORDINGS_VISUAL_PROXY_PORT"));
    const char *path=getenv("RECORDINGS_VISUAL_ATTEST_PATH");
    if(!port || !path || path[0]!='/' || strlen(path)>240 || strspn(path,"/abcdef0123456789-")!=strlen(path)) _exit(126);
    real_connect=dlsym(RTLD_NEXT,"connect");
    int fd=socket(AF_INET,SOCK_STREAM,0);if(fd<0)_exit(126);
    struct timeval timeout={5,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
    struct sockaddr_in addr={.sin_family=AF_INET,.sin_port=htons(port),.sin_addr.s_addr=htonl(INADDR_LOOPBACK)};
    if(connect(fd,(struct sockaddr*)&addr,sizeof(addr)))_exit(126);
    char request[512],response[64];int n=snprintf(request,sizeof(request),"GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",path);
    if(send(fd,request,n,MSG_NOSIGNAL)!=n)_exit(126);
    int got=recv(fd,response,sizeof(response)-1,0);close(fd);
    if(got<12)_exit(126);
    response[got]=0;
    if(strncmp(response,"HTTP/1.1 204",12))_exit(126);
}
