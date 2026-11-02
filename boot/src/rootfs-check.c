#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
static uint16_t u16(const unsigned char *p){return (uint16_t)(p[0]|p[1]<<8);}
static uint32_t u32(const unsigned char *p){return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static int fail(const char *message){fprintf(stderr,"FRL_EXT4_REFUSED: %s\n",message);return 78;}
int main(int argc,char **argv){
 if(argc!=4 || (strcmp(argv[3],"staged") && strcmp(argv[3],"accepted")))return fail("arguments");
 if(strlen(argv[2])!=32)return fail("UUID length");
 for(int i=0;i<32;i++)if(!((argv[2][i]>='0'&&argv[2][i]<='9')||(argv[2][i]>='a'&&argv[2][i]<='f')))return fail("UUID encoding");
 int fd=open(argv[1],O_RDONLY|O_NOFOLLOW|O_CLOEXEC);if(fd<0)return fail("open");
 struct stat st;unsigned char s[1024];
 if(fstat(fd,&st)||!S_ISREG(st.st_mode)||st.st_uid||st.st_nlink!=1||st.st_size!=4294967296LL){close(fd);return fail("file identity/size");}
 ssize_t got=pread(fd,s,sizeof s,1024);close(fd);if(got!=sizeof s)return fail("superblock read");
 if(u16(s+56)!=0xef53||u32(s+24)!=2||u32(s+4)!=1048576||u32(s+336))return fail("geometry");
 uint32_t compat=u32(s+92),incompat=u32(s+96),ro=u32(s+100);
 if(!(compat&4)||incompat&~0x246u||ro&~0x46bu||compat&0x1000u)return fail("unsupported ext4 feature");
 uint16_t state=u16(s+58);
 if(state&2)return fail("filesystem error flag");
 if(!strcmp(argv[3],"staged")&&(!(state&1)||(incompat&4)))return fail("staged filesystem not clean");
 char uuid[33];for(int i=0;i<16;i++)snprintf(uuid+i*2,3,"%02x",s[104+i]);
 if(strcmp(uuid,argv[2]))return fail("filesystem UUID");
 printf("FRL_EXT4_VALID uuid=%s mode=%s state=%x features=%x/%x/%x\n",uuid,argv[3],state,compat,incompat,ro);return 0;
}
